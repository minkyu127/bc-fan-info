"""데이터 자동 수집기.

출처
- 블랙컴뱃 공식 홈페이지 (blackcombat-official.com): 랭킹, 선수 프로필
- 나무위키 (namu.wiki): 대회 일정/결과, 선수 통산 전적, 블랙 테이블 영입

예의 있게 수집하기 위해 요청 사이에 쉬는 시간을 두고,
한 번 받은 페이지는 data/cache 폴더에 저장해 두고 다시 씁니다.
"""
import hashlib
import os
import re
import threading
import time
import traceback
from datetime import date, datetime
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

import db as dbm

OFFICIAL = "https://www.blackcombat-official.com"
NAMU = "https://namu.wiki/w/"
CACHE_DIR = os.path.join(dbm.DATA_DIR, "cache")
REQUEST_DELAY = 1.2  # 초. 서버에 부담을 주지 않도록 요청 사이 대기
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BlackCombatFanViewer/1.0; personal use)",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

WEIGHT_ORDER = ["플라이급", "밴텀급", "페더급", "라이트급", "웰터급", "미들급", "헤비급"]

# ---------------------------------------------------------------- 진행 상태

STATUS = {"running": False, "step": "", "done": 0, "total": 0, "log": [],
          "error": None, "finished_at": None, "mode": None}
_lock = threading.Lock()


def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    STATUS["log"].append(f"[{stamp}] {msg}")
    STATUS["log"] = STATUS["log"][-200:]
    print(msg, flush=True)


def step(name, total=0):
    STATUS["step"] = name
    STATUS["done"] = 0
    STATUS["total"] = total
    log(name)


# ---------------------------------------------------------------- 내려받기

class Fetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.last = 0.0
        os.makedirs(CACHE_DIR, exist_ok=True)

    def get(self, url, ttl_hours=6):
        path = os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".html")
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl_hours * 3600:
            with open(path, encoding="utf-8") as f:
                return f.read()
        wait = REQUEST_DELAY - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        try:
            r = self.session.get(url, timeout=25)
        except requests.RequestException as e:
            log(f"  ! 연결 실패: {url} ({e.__class__.__name__})")
            return self._stale(path)
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            log(f"  ! 응답 {r.status_code}: {unquote(url)}")
            return self._stale(path)
        r.encoding = "utf-8"
        with open(path, "w", encoding="utf-8") as f:
            f.write(r.text)
        return r.text

    @staticmethod
    def _stale(path):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        return None

    def namu(self, title, ttl_hours=6):
        return self.get(NAMU + quote(title), ttl_hours)


# ---------------------------------------------------------------- 도우미

def soup(html):
    s = BeautifulSoup(html, "html.parser")
    for t in s(["script", "style"]):
        t.decompose()
    return s


def clean(text):
    text = re.sub(r"\[\d+\]", "", text or "")          # 각주 [1]
    text = re.sub(r"\[편집\]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def norm(text):
    return re.sub(r"[\s\-_.·'\"()]", "", (text or "")).lower()


def cells_of(row):
    return row.find_all(["td", "th"], recursive=False)


def namu_link_title(a):
    href = a.get("href", "")
    if not href.startswith("/w/"):
        return None
    return unquote(href[3:]).split("#")[0]


SKIP_LINK_TEXT = re.compile(r"^(#?\d+|C|IC|[WLD]|NC|\[\d+\])$")


def fighter_link(cell):
    """표 칸에서 선수 이름 링크(이름, 나무위키 문서명)를 찾음."""
    for a in cell.find_all("a", href=True):
        text = clean(a.get_text(" ", strip=True))
        title = namu_link_title(a)
        if not text or not title or SKIP_LINK_TEXT.match(text):
            continue
        if "랭킹" in title:
            continue
        return text, title
    return None, None


def outcome_of(result):
    r = result or ""
    if "무효" in r or "NC" in r.upper() or "노 콘테스트" in r:
        return "NC"
    if "무승부" in r or re.search(r"무$", r):
        return "D"
    if "패" in r:
        return "L"
    if "승" in r:
        return "W"
    return ""


def method_of(result):
    r = (result or "").upper()
    if "KO" in r:
        return "KO/TKO"
    if "SUB" in r or "서브미션" in r or "초크" in r or "암바" in r or "SUBMISSION" in r:
        return "서브미션"
    if "판정" in r or "DECISION" in r:
        return "판정"
    return "기타"


def series_of(name):
    n = name or ""
    if "라이즈" in n:
        return "라이즈"
    if "블랙컵" in n:
        return "블랙컵"
    if "챔피언스" in n or re.search(r"\bC\.?L\b", n):
        return "챔피언스 리그"
    if "오디션" in n:
        return "프로 오디션"
    if "비긴즈" in n:
        return "비긴즈"
    if re.search(r"블랙컴뱃\s*\d+", n):
        return "넘버링"
    return "기타"


# ---------------------------------------------------------------- 공식 사이트

def parse_official_ranking(html):
    """공식 랭킹 페이지 → {체급: [{rank, url, ring_name, name_en, country, record, change, photo}]}"""
    s = soup(html)
    result = {}
    for part in s.find_all("div", class_="ranking_list_part"):
        champ = part.find("div", class_="ranking_list_part_champ")
        weight_el = part.find("span", class_="weight")
        if not weight_el:
            continue
        weight = clean(weight_el.get_text())
        extra = " ".join(c for c in part.get("class", []) if c != "ranking_list_part").lower()
        if weight in result or "women" in extra or "female" in extra or "여성" in weight:
            if not weight.startswith("여성부"):
                weight = "여성부 " + weight.replace("여성", "").strip()
        entries = []
        if champ:
            url = _onclick_url(champ)
            name_box = champ.find("div", class_="ranking_champ_name")
            ring = name_box.find("span", class_="fighter_name") if name_box else None
            subs = name_box.find_all("div", class_="ranking_ring_name") if name_box else []
            img = champ.select_one(".ranking_champ_photo img")
            if url and ring:
                entries.append({
                    "rank": 0, "url": url, "ring_name": clean(ring.get_text()),
                    "name_en": clean(subs[0].get_text()) if subs else "",
                    "record": clean(subs[1].get_text()) if len(subs) > 1 else "",
                    "country": _flag(name_box), "change": "",
                    "photo": img.get("src") if img else None,
                })
        for item in part.find_all("div", class_="ranking_list_part_item"):
            url = _onclick_url(item)
            num = item.find("div", class_="ranking_list_num")
            if not url or not num:
                continue
            try:
                rank = int(clean(num.get_text()))
            except ValueError:
                continue
            ring = item.find("span", class_="fighter_name")
            real = item.find("span", class_="ring_name")
            score = item.find("div", class_="ranking_list_score")
            change = item.find("div", class_="ranking_list_change")
            img = item.select_one(".ranking_list_photo img")
            entries.append({
                "rank": rank, "url": url,
                "ring_name": clean(ring.get_text()) if ring else "",
                "name_en": clean(real.get_text()) if real else "",
                "record": clean(score.get_text()) if score else "",
                "country": _flag(item),
                "change": clean(change.get_text()) if change else "",
                "photo": img.get("src") if img else None,
            })
        result[weight] = entries
    return result


def _onclick_url(el):
    m = re.search(r"location\.href='([^']+)'", el.get("onclick", ""))
    return m.group(1) if m else None


def _flag(el):
    f = el.find("span", class_=re.compile(r"^fi-"))
    if not f:
        return ""
    for c in f.get("class", []):
        if c.startswith("fi-") and len(c) <= 6:
            return c[3:]
    return ""


def parse_official_fighter(html):
    s = soup(html)
    out = {}
    tag = s.select_one(".data_tags span")
    if tag:
        out["weight_tag"] = clean(tag.get_text())
    age = s.find("span", class_="data_age")
    if age:
        m = re.search(r"(\d+)", age.get_text())
        out["age"] = int(m.group(1)) if m else None
    team = s.select_one(".data_team b")
    if team:
        out["team"] = clean(team.get_text())
    h = s.find("div", class_="data_bio_height")
    if h:
        val = clean(h.get_text()).replace("HEIGHT", "").strip()
        out["height"] = val if val and val != "-" else None
    rec = s.find("div", class_="data_record")
    if rec:
        for box in rec.find_all("div", recursive=False):
            mini = box.find("div", class_="mini")
            label = clean(mini.get_text()).lower() if mini else ""
            m = re.search(r"\d+", box.get_text())
            n = int(m.group()) if m else 0
            if label == "win":
                out["wins"] = n
            elif label == "loss":
                out["losses"] = n
            elif label == "draw":
                out["draws"] = n
    img = s.select_one(".fighter_img img")
    if img:
        out["photo"] = img.get("src")
    return out


# ---------------------------------------------------------------- 나무위키

def parse_namu_ranking(html):
    """나무위키 '블랙컴뱃 랭킹' → {체급: [{rank, name, namu_title, ring_name, total, bc, note, country_ko}]}"""
    s = soup(html)
    result = {}
    for t in s.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) < 3:
            continue
        head = clean(rows[0].get_text(" "))
        m = re.search(r"블랙컴뱃\s*(\S+급)\s*랭킹", head)
        if not m or "순위" not in rows[1].get_text():
            continue
        weight = m.group(1)
        if weight in result:
            weight = "여성부"  # 두 번째로 나오는 같은 체급 = 여성부 (공식 사이트와 같은 이름)
        entries = []
        for r in rows[2:]:
            cs = cells_of(r)
            if len(cs) < 3:
                continue
            rank_txt = clean(cs[0].get_text())
            if rank_txt == "C":
                rank = 0
            elif rank_txt.isdigit():
                rank = int(rank_txt)
            else:
                continue
            name, title = fighter_link(cs[1])
            if not name:
                name = clean(cs[1].get_text())
            country = ""
            for a in cs[1].find_all("a", href=True):
                if not a.get_text(strip=True) and namu_link_title(a):
                    country = namu_link_title(a)
                    break
            rest = [clean(c.get_text(" ")) for c in cs[3:]]
            records = [x for x in rest if re.match(r"^\d+전", x)]
            others = [x for x in rest if x not in records]
            entries.append({
                "rank": rank, "name": name, "namu_title": title,
                "ring_name": clean(cs[2].get_text()),
                "total": records[0] if records else "",
                "bc": records[1] if len(records) > 1 else "",
                "note": " ".join(o for o in others if o != "-"),
                "country_ko": country,
            })
        result[weight] = entries
    return result


def parse_namu_fighter(html):
    s = soup(html)
    info = {}
    for t in s.find_all("table"):
        for r in t.find_all("tr"):
            cs = cells_of(r)
            if len(cs) != 2:
                continue
            key = clean(cs[0].get_text())
            val = clean(cs[1].get_text(" "))
            if key == "출생" and "birth" not in info:
                m = re.search(r"\d{4}년\s*\d{1,2}월\s*\d{1,2}일", val)
                info["birth"] = m.group() if m else val[:30]
            elif key == "국적" and "country_ko" not in info:
                info["country_ko"] = val[:20]
            elif key == "신체" and "height" not in info:
                m = re.search(r"(\d{3}(?:\.\d)?)\s*cm", val)
                if m:
                    info["height"] = m.group(1) + "cm"
            elif key in ("종합격투기 전적", "전적") and "record_text" not in info:
                info["record_text"] = val
        if "birth" in info or "country_ko" in info:
            break
    info["fights"] = _parse_record_table(s)
    return info


def _parse_record_table(s):
    for t in s.find_all("table"):
        rows = t.find_all("tr")
        if not rows:
            continue
        header = [clean(c.get_text()) for c in cells_of(rows[0])]
        if "상대" not in header or not any("결과" in h for h in header):
            continue
        if not any(h in ("경기일", "날짜", "일자") for h in header):
            continue
        idx = {h: i for i, h in enumerate(header)}
        col_date = next((idx[h] for h in ("경기일", "날짜", "일자") if h in idx), None)
        col_event = next((idx[h] for h in ("대회명", "대회", "이벤트") if h in idx), None)
        col_opp = idx["상대"]
        col_res = next(i for i, h in enumerate(header) if "결과" in h)
        col_note = idx.get("비고")
        fights = []
        for r in rows[1:]:
            cs = cells_of(r)
            if len(cs) != len(header):
                continue
            g = lambda i: clean(cs[i].get_text(" ")) if i is not None else ""
            result = g(col_res)
            fights.append({
                "seq": len(fights) + 1, "date": g(col_date), "event": g(col_event),
                "opponent": g(col_opp), "result_text": result,
                "outcome": outcome_of(result), "method": method_of(result),
                "note": g(col_note),
            })
        if fights:
            return fights
    return []


EVENT_DATE = re.compile(r"(?:(\d{4})년\s*)?(\d{1,2})월\s*(\d{1,2})일")


def parse_namu_year(html, year):
    """나무위키 '블랙컴뱃/2026년' → 대회 목록"""
    s = soup(html)
    events = []
    for t in s.find_all("table"):
        rows = t.find_all("tr")
        hdr_i = None
        for i, r in enumerate(rows[:3]):
            heads = [clean(c.get_text()) for c in cells_of(r)]
            if "대회" in heads and "날짜" in heads:
                hdr_i, header = i, heads
                break
        if hdr_i is None:
            continue
        ci = {h: i for i, h in enumerate(header)}
        for r in rows[hdr_i + 1:]:
            cs = cells_of(r)
            if len(cs) != len(header):
                continue
            name_cell = cs[ci["대회"]]
            name = clean(name_cell.get_text(" "))
            a = name_cell.find("a", href=True)
            title = namu_link_title(a) if a else None
            d = clean(cs[ci["날짜"]].get_text())
            m = EVENT_DATE.search(d)
            iso = None
            if m:
                y = int(m.group(1) or year)
                try:
                    iso = date(y, int(m.group(2)), int(m.group(3))).isoformat()
                except ValueError:
                    iso = None
            events.append({
                "id": title or name, "name": name, "date": iso,
                "venue": clean(cs[ci["장소"]].get_text(" ")) if "장소" in ci else "",
                "main_event": clean(cs[ci["메인이벤트"]].get_text(" ")) if "메인이벤트" in ci else "",
                "namu_title": title, "series": series_of(name),
            })
        if events:
            break
    return events


def parse_scorecard(text):
    """'( 알리 가지예프 30-26, 30-26, 30-27)' → '30-26, 30-26, 30-27' (판정일 때만 있음)"""
    scores = re.findall(r"\b(\d{1,2})\s*[-:]\s*(\d{1,2})\b", text or "")
    scores = [f"{a}-{b}" for a, b in scores if 7 <= int(a) <= 50 and 7 <= int(b) <= 50]
    return ", ".join(scores[:3])


BOUT_RE = re.compile(r"\(\s*[-+]?\d+(?:\.\d+)?\s*kg\s*\)")
RESULT_RE = re.compile(r"(승|무승부|무효|취소|노 콘테스트)")


def parse_namu_event(html):
    """대회 문서 → 경기 목록 [{bout_label, a, a_title, b, b_title, result, winner}]"""
    s = soup(html)
    fights, seen = [], set()
    for t in s.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) > 12:
            continue
        for i, r in enumerate(rows):
            label = clean(r.get_text(" "))
            if len(label) > 60 or not BOUT_RE.search(label) or i + 1 >= len(rows):
                continue
            cs = cells_of(rows[i + 1])
            if len(cs) < 2:
                continue
            a, at = fighter_link(cs[0])
            b, bt = fighter_link(cs[-1])
            if not a or not b:
                continue
            result, winner, score = "", "", ""
            for r2 in rows[i + 2:i + 4]:
                txt = clean(r2.get_text(" "))
                if "보너스" in txt or "다시보기" in txt:
                    continue
                if RESULT_RE.search(txt):
                    result, _, after = txt.partition("→")
                    result = result.strip()
                    score = parse_scorecard(after)
                    w = r2.find("a", href=True)
                    winner = clean(w.get_text()) if w else result.split(" ")[0]
                    break
            key = (a, b)
            if key in seen:
                continue
            seen.add(key)
            fights.append({"bout_label": label, "a": a, "a_title": at, "b": b,
                           "b_title": bt, "result": result, "winner": winner, "score": score})
    # 형식 B (예전 대회 문서): 행 머리가 '선수' / '체급' / '결과'인 경기별 표
    for t in s.find_all("table"):
        rows = t.find_all("tr")
        if len(rows) > 12 or t.find("table"):
            continue
        heads = {}
        title_txt = ""
        for r in rows:
            cs = cells_of(r)
            if not cs:
                continue
            key = clean(cs[0].get_text(" "))
            if key in ("선수", "체급", "결과", "승 / 패", "승/패") and len(cs) >= 2:
                heads[key] = cs[1:]
            elif not title_txt and len(cs) == 1:
                title_txt = key
            elif len(cs) == 1 and re.search(r"(제\s*\d+\s*경기|MAIN EVENT|메인 ?이벤트|코메인)", key, re.I):
                title_txt = (title_txt + " " + key).strip()
        if "선수" not in heads or len(heads["선수"]) < 2:
            continue
        a, at = fighter_link(heads["선수"][0])
        b, bt = fighter_link(heads["선수"][-1])
        if not a:
            a = _plain_name(heads["선수"][0])
        if not b:
            b = _plain_name(heads["선수"][-1])
        if not a or not b or (a, b) in seen:
            continue
        seen.add((a, b))
        weight = clean(" ".join(c.get_text(" ") for c in heads.get("체급", [])))
        m = re.search(r"(제\s*\d+\s*경기|MAIN EVENT|메인 ?이벤트|코메인 ?이벤트)", title_txt, re.I)
        label = " ".join(x for x in [m.group(1) if m else "", weight] if x).strip()
        result = clean(" ".join(c.get_text(" ") for c in heads.get("결과", [])))
        result = result.strip("〈〉<> ").replace("〈", "").replace("〉", "").strip()
        winner = ""
        wm = re.match(r"^(.+?)\s+(?:연장\s*)?\d+\s*R", result)
        wname = wm.group(1).strip() if wm else ""
        for side in ("a", "b"):
            nm = a if side == "a" else b
            raw = _cell_text(heads["선수"][0 if side == "a" else -1])
            if result.startswith(nm):
                winner = nm
            elif wname and wname in raw:
                winner = wname
                if side == "a" and not at:
                    a = wname
                elif side == "b" and not bt:
                    b = wname
        fights.append({"bout_label": label, "a": a, "a_title": at, "b": b, "b_title": bt,
                       "result": result, "winner": winner})
    if fights:
        return fights
    # 결과가 아직 없는 예정 대회: 대진표(3칸 행: 선수 | 경기 | 선수)에서 가져옴
    for t in s.find_all("table"):
        for r in t.find_all("tr"):
            cs = cells_of(r)
            if len(cs) != 3:
                continue
            label = clean(cs[1].get_text(" "))
            if not BOUT_RE.search(label) or len(label) > 60:
                continue
            a, at = fighter_link(cs[0])
            b, bt = fighter_link(cs[2])
            if a and b and (a, b) not in seen:
                seen.add((a, b))
                fights.append({"bout_label": label, "a": a, "a_title": at, "b": b,
                               "b_title": bt, "result": "", "winner": ""})
    return fights


VS_DATE = re.compile(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})")


def parse_versus(html):
    """나무위키 '블랙컴뱃' 문서의 '대항전' 부분 → 대항전 목록 (경기 포함)"""
    s = soup(html)
    start = next((h for h in s.find_all("h2") if "대항전" in h.get_text()), None)
    if not start:
        return []
    out = []
    for el in start.find_all_next(["h2", "table"]):
        if el.name == "h2":
            break
        if el.find_parent("table"):
            continue
        rows = el.find_all("tr")
        if len(rows) < 4:
            continue
        title = clean(rows[0].get_text(" "))
        if "vs" not in title.lower():
            continue
        title = re.sub(r"\s*<\s*(.*?)\s*>\s*", r" \1", title).strip()  # '< 1차전 >' → ' 1차전'
        text = clean(el.get_text(" "))
        m = VS_DATE.search(text)
        iso = None
        if m:
            try:
                iso = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                pass
        venue, score, label_carry, fights = "", "", "", []
        for r in rows[1:]:
            cs = cells_of(r)
            txt = clean(r.get_text(" "))
            if len(cs) == 1 and not venue and not re.search(r"(VS|펼치기|경기|중계|대표|링 아나운서|게임)", txt) \
                    and len(txt) < 40:
                venue = txt
                continue
            if len(cs) == 1 and len(txt) < 12:
                label_carry = txt  # '미니 게임', '2 대 2 경기' 같은 구분
                continue
            mid = clean(cs[1].get_text(" ")) if len(cs) >= 3 else ""
            if re.match(r"^(\d+M\s*\d+R\s*)?\d+\s*:\s*\d+$", mid) and not score:
                left, right = clean(cs[0].get_text(" ")), clean(cs[-1].get_text(" "))
                sc = re.search(r"(\d+)\s*:\s*(\d+)\s*$", clean(cs[1].get_text(" ")))
                if sc:
                    score = f"{left.split(' ')[0]} {sc.group(1)} : {sc.group(2)} {right.split(' ')[0]}"
                continue
            if len(cs) >= 2:
                lt, rt = clean(cs[0].get_text(" ")), clean(cs[-1].get_text(" "))
                lm, rm = re.search(r"\((.*?)\)\s*$", lt), re.search(r"\((.*?)\)\s*$", rt)
                if not lm or not rm:
                    continue
                a, b = lt[:lm.start()].strip(), rt[:rm.start()].strip()
                label = clean(cs[1].get_text(" ")).split("주심")[0].strip() if len(cs) >= 3 else label_carry
                if label_carry and label_carry not in label:
                    label = (label_carry + " " + label).strip()
                if "승" in lm.group(1) and "패" != lm.group(1):
                    winner, res = a, lm.group(1)
                elif "승" in rm.group(1):
                    winner, res = b, rm.group(1)
                else:
                    winner, res = "", lm.group(1)
                fights.append({"bout_label": label, "a": a, "a_title": None, "b": b, "b_title": None,
                               "result": f"{winner} {res}".strip() if winner else res, "winner": winner})
        out.append({"id": "대항전:" + title, "name": title, "date": iso, "venue": venue,
                    "main_event": score, "fights": fights})
    return out


CL_SEASONS = ["2022-23", "2024-25", "2026-27"]  # 없는 시즌 문서는 건너뜀


def parse_cl_season(html, season):
    """'블랙컴뱃/챔피언스 리그/2022-23시즌' → 회차별 대회 (날짜·팀 점수·경기 결과)"""
    s = soup(html)
    out = []
    for h in s.find_all("h3"):
        m = re.match(r"^[\d.]+\s*(\d+)\s*회차\s*/\s*(.+)$", clean(h.get_text(" ")))
        if not m:
            continue
        rnd, matchup = int(m.group(1)), m.group(2).strip()
        tables = []
        for el in h.find_all_next(["h2", "h3", "table"]):
            if el.name in ("h2", "h3"):
                break
            if not el.find_parent("table"):
                tables.append(el)
        iso, venue, score, teams, fights = None, "", "", [], []
        for t in tables:
            txt = clean(t.get_text(" ")).replace("✊", " ")
            if "CHAMPIONS LEAGUE" not in txt.upper():
                continue
            dm = re.search(r"(\d{4})\.\s+(\d{1,2})\.\s+(\d{1,2})", txt)  # '2022. 10. 1' (경기 결과 표의 날짜)
            if dm and not iso:
                try:
                    iso = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3))).isoformat()
                except ValueError:
                    pass
            rows = t.find_all("tr")
            for i, r in enumerate(rows):
                cs = cells_of(r)
                rt = clean(r.get_text(" ")).replace("✊", " ").strip()
                if i == 1 and len(cs) == 1:
                    venue = rt.split("|")[-1].strip()
                sc = re.search(r"(\d+)\s*:\s*(\d+)\s*$", rt) if "라운드" in rt or len(cs) == 3 and not cs[0].get_text(strip=True) else None
                if sc and not score:
                    score = f"{sc.group(1)} : {sc.group(2)}"
                    if i + 1 < len(rows):
                        teams = [clean(c.get_text(" ")) for c in cells_of(rows[i + 1])]
                    continue
                if len(cs) < 2:
                    continue
                lt = clean(cs[0].get_text(" ")).replace("✊", " ").strip()  # ✊ = 승자 표시
                rt2 = clean(cs[-1].get_text(" ")).replace("✊", " ").strip()
                lm = re.search(r"\((승|패|무)[^)]*\)\s*$", lt)
                rm = re.search(r"\((승|패|무)[^)]*\)\s*$", rt2)
                if not lm or not rm:
                    continue
                label = clean(cs[1].get_text(" ")).split("주심")[0].strip() if len(cs) >= 3 else ""
                a_part, b_part = lt[:lm.start()].strip(), rt2[:rm.start()].strip()
                def split_how(part):
                    """'옥은찬 라텔 1R 3:24 TKO' → ('옥은찬 라텔', '1R 3:24 TKO')"""
                    mm = re.search(r"(\d+\s*R.*|[^ ]*판정.*)$", part)
                    return (part[:mm.start()].strip(), mm.group(1).strip()) if mm else (part, "")
                a, a_how = split_how(a_part)  # 이름+링네임 (저장할 때 선수 목록과 맞춰 봄)
                b, b_how = split_how(b_part)

                def res(name, h):
                    first = name.split(" ")[0]
                    return f"{first} {h}" if h.endswith("승") else f"{first} {h} 승".replace("  ", " ")
                if lm.group(1) == "승":
                    winner, result = a, res(a, a_how)
                elif rm.group(1) == "승":
                    winner, result = b, res(b, b_how)
                else:
                    winner, result = "", "무승부"
                fights.append({"bout_label": label, "a": a, "a_title": None, "b": b, "b_title": None,
                               "result": result, "winner": winner})
        main = f"{teams[0]} {score} {teams[-1]}" if score and len(teams) >= 2 else matchup
        out.append({"id": f"CL:{season}:{rnd:02d}", "name": f"{season} 챔피언스 리그 {rnd}회차: {matchup}",
                    "date": iso, "venue": venue, "main_event": main, "fights": fights})
    return out


def _cell_text(cell):
    """'#3 링네임 홍길동' → '링네임 홍길동' (순위 표시 제거)"""
    txt = clean(cell.get_text(" "))
    txt = re.sub(r"^[A-Z]*-?#\s*(C|IC|\d+)\s*", "", txt)
    return txt.strip()


def _plain_name(cell):
    """링크가 없는 칸: 이름과 링네임이 섞인 글 그대로 (저장할 때 선수 목록과 맞춰 봄)"""
    return _cell_text(cell)


HEADING_RE = re.compile(r"^[\d.]+\s*(.*?)\s*(?:【(.*?)】)?\s*$")


def parse_black_table(html):
    """나무위키 '블랙 테이블' / '블랙리스트(블랙컴뱃)' → 영입 선수 목록
    소제목(h3)이 '2025년' 또는 '1차' 같은 묶음, 그 아래 h4가 '이름 【링네임】'"""
    s = soup(html)
    out, year = [], ""
    for h in s.find_all(["h2", "h3", "h4"]):
        text = clean(h.get_text(" "))
        m = HEADING_RE.match(text)
        if not m:
            continue
        title, ring = m.group(1), m.group(2)
        if h.name in ("h2", "h3") and re.match(r"^(\d{4}년|\d+차)$", title):
            year = title[:-1] if title.endswith("년") else title
            continue
        title = re.sub(r"\s*\[[^\]]*\]", "", title).strip()  # '[은퇴]' 같은 표시 제거
        if h.name == "h4" and ring and year:
            desc = ""
            t = h.find_next("table")
            if t:
                rows = t.find_all("tr")
                lines = [clean(r.get_text(" ")) for r in rows if clean(r.get_text(" "))]
                desc = lines[-1] if lines else ""
                desc = re.sub(r"\s*@\S+", "", desc).strip()  # SNS 아이디 제거
            out.append({"year": year, "name": title, "ring_name": ring,
                        "description": desc[:300]})
    return out


# ---------------------------------------------------------------- 전체 흐름

def run_update(mode="quick"):
    """mode: quick(랭킹·일정·영입) / full(+모든 랭킹 선수 상세 정보)"""
    with _lock:
        if STATUS["running"]:
            return False
        STATUS.update(running=True, error=None, log=[], mode=mode, finished_at=None)
    threading.Thread(target=_run, args=(mode,), daemon=True).start()
    return True


def _run(mode):
    f = Fetcher()
    try:
        dbm.init_db()
        with dbm.get_db() as db:
            update_rankings(db, f)
            db.commit()
            if mode == "full":
                update_fighter_details(db, f)
                db.commit()
            update_events(db, f)
            db.commit()
            update_signings(db, f)
            db.commit()
            update_news(db, f)
            dbm.set_meta(db, "last_update", datetime.now().strftime("%Y-%m-%d %H:%M"))
            dbm.set_meta(db, "last_mode", mode)
            dbm.set_meta(db, "last_update_ts", time.time())
            if mode == "full":
                dbm.set_meta(db, "last_full_ts", time.time())
        log("✅ 업데이트 완료!")
    except Exception as e:  # 어떤 오류든 화면에 보여주기 위해
        STATUS["error"] = f"{e.__class__.__name__}: {e}"
        log("❌ 오류: " + STATUS["error"])
        traceback.print_exc()
    finally:
        STATUS["running"] = False
        STATUS["finished_at"] = datetime.now().strftime("%H:%M:%S")


def update_rankings(db, f):
    step("① 공식 홈페이지 랭킹 가져오는 중")
    html = f.get(OFFICIAL + "/ranking.php?type=fighter", ttl_hours=3)
    official = parse_official_ranking(html) if html else {}
    step("② 나무위키 랭킹 가져오는 중")
    nhtml = f.namu("블랙컴뱃 랭킹", ttl_hours=6)
    namu = parse_namu_ranking(nhtml) if nhtml else {}
    log(f"  공식 {sum(len(v) for v in official.values())}명 / 나무위키 {sum(len(v) for v in namu.values())}명")

    if not official and not namu:
        raise RuntimeError("랭킹을 가져오지 못했습니다. 인터넷 연결을 확인하세요.")

    now = datetime.now().isoformat(timespec="seconds")
    db.execute("DELETE FROM rankings")
    weights = list(official.keys()) or list(namu.keys())
    for w in namu:
        if w not in weights:
            weights.append(w)
    for order, w in enumerate(weights):
        off = official.get(w, [])
        nm = namu.get(w, [])
        by_ring = {norm(e["ring_name"]): e for e in nm}
        by_name = {norm(e["name"]): e for e in nm}
        by_rank = {e["rank"]: e for e in nm}
        used = set()
        for e in off:
            n = by_ring.get(norm(e["ring_name"])) or by_name.get(norm(e["name_en"]))
            if not n:
                cand = by_rank.get(e["rank"])
                if cand and not cand.get("_used"):
                    n = cand if norm(cand["ring_name"])[:2] == norm(e["ring_name"])[:2] else None
            if n:
                n["_used"] = True
                used.add(id(n))
            fid = e["url"].rstrip("/").split("/")[-1]
            _upsert_fighter(db, fid, {
                "name": (n or {}).get("name") or e["name_en"],
                "name_en": e["name_en"], "ring_name": e["ring_name"], "weight_class": w,
                "country": e["country"],
                "record_text": (n or {}).get("total"), "bc_record_text": (n or {}).get("bc"),
                "note": (n or {}).get("note"), "official_url": e["url"],
                "namu_title": (n or {}).get("namu_title"), "photo_url": e["photo"],
                "updated_at": now, **_wld(e["record"]),
            })
            db.execute("INSERT OR REPLACE INTO rankings VALUES (?,?,?,?,?)",
                       (w, order, e["rank"], fid, e["change"]))
        # 공식 사이트에 없는 나무위키 랭킹 선수
        for n in nm:
            if id(n) in used or not n.get("namu_title"):
                continue
            existing = db.execute("SELECT id FROM fighters WHERE namu_title=?",
                                  (n["namu_title"],)).fetchone()
            fid = existing["id"] if existing else "n:" + n["namu_title"]
            _upsert_fighter(db, fid, {
                "name": n["name"], "ring_name": n["ring_name"], "weight_class": w,
                "record_text": n["total"], "bc_record_text": n["bc"], "note": n["note"],
                "namu_title": n["namu_title"], "updated_at": now,
                **_wld_ko(n["total"]),
            })
            if not official:
                db.execute("INSERT OR REPLACE INTO rankings VALUES (?,?,?,?,?)",
                           (w, order, n["rank"], fid, ""))


def _wld(text):
    m = re.match(r"\s*(\d+)\s*/\s*(\d+)\s*/\s*(\d+)", text or "")
    if not m:
        return {}
    return {"wins": int(m.group(1)), "losses": int(m.group(2)), "draws": int(m.group(3))}


def _wld_ko(text):
    t = text or ""
    g = lambda pat: int(m.group(1)) if (m := re.search(pat, t)) else 0
    if not t:
        return {}
    return {"wins": g(r"(\d+)승"), "losses": g(r"(\d+)패"), "draws": g(r"(\d+)무")}


def _upsert_fighter(db, fid, data):
    data = {k: v for k, v in data.items() if v not in (None, "")}
    row = db.execute("SELECT id FROM fighters WHERE id=?", (fid,)).fetchone()
    if row:
        sets = ", ".join(f"{k}=?" for k in data)
        db.execute(f"UPDATE fighters SET {sets} WHERE id=?", (*data.values(), fid))
    else:
        keys = ["id", *data.keys()]
        db.execute(f"INSERT INTO fighters ({','.join(keys)}) VALUES ({','.join('?' * len(keys))})",
                   (fid, *data.values()))


def update_fighter_details(db, f):
    fighters = db.execute(
        "SELECT DISTINCT f.id, f.official_url, f.namu_title, f.name FROM fighters f "
        "JOIN rankings r ON r.fighter_id=f.id").fetchall()
    step("③ 선수 상세 정보 가져오는 중 (처음엔 몇 분 걸려요)", len(fighters))
    for i, fr in enumerate(fighters, 1):
        STATUS["done"] = i
        data = {}
        if fr["official_url"]:
            html = f.get(fr["official_url"], ttl_hours=24)
            if html:
                o = parse_official_fighter(html)
                data.update({k: o.get(k) for k in ("age", "team", "height", "wins", "losses", "draws")})
                if o.get("photo"):
                    data["photo_url"] = o["photo"]
        fights = []
        if fr["namu_title"]:
            html = f.namu(fr["namu_title"], ttl_hours=24)
            if html:
                n = parse_namu_fighter(html)
                for k in ("birth", "record_text"):
                    if n.get(k):
                        data[k] = n[k]
                if n.get("height") and not data.get("height"):
                    data["height"] = n["height"]
                fights = n["fights"]
        _upsert_fighter(db, fr["id"], data)
        if fights:
            db.execute("DELETE FROM fighter_fights WHERE fighter_id=?", (fr["id"],))
            db.executemany(
                "INSERT INTO fighter_fights VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(fr["id"], x["seq"], x["date"], x["event"], x["opponent"], x["result_text"],
                  x["outcome"], x["method"], x["note"], "namu") for x in fights])
        if i % 10 == 0:
            log(f"  {i}/{len(fighters)}명 완료")
            db.commit()


FIGHT_INSERT = ("INSERT INTO event_fights (event_id, bout_order, bout_label, fighter_a, fighter_a_id, "
                "fighter_b, fighter_b_id, result_text, winner, scorecard) VALUES (?,?,?,?,?,?,?,?,?,?)")


def update_events(db, f):
    step("④ 대회 일정·결과 가져오는 중")
    this_year = date.today().year
    all_events = []
    for y in range(2022, this_year + 2):
        html = f.namu(f"블랙컴뱃/{y}년", ttl_hours=6 if y >= this_year else 24 * 14)
        if not html:
            continue
        evs = parse_namu_year(html, y)
        log(f"  {y}년: 대회 {len(evs)}개")
        all_events.extend(evs)
    if not all_events:
        log("  ! 대회 목록을 찾지 못했습니다 (기존 데이터 유지)")
        return

    db.execute("DELETE FROM events WHERE source='namu'")
    for e in all_events:
        db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?)",
                   (e["id"], e["name"], e["date"], e["venue"], e["main_event"],
                    e["series"], e["namu_title"], "namu"))

    title_map = {r["namu_title"]: r["id"] for r in
                 db.execute("SELECT id, namu_title FROM fighters WHERE namu_title IS NOT NULL")}
    name_map, ring_map = {}, {}
    for r in db.execute("SELECT id, name, ring_name FROM fighters"):
        if r["name"]:
            name_map.setdefault(r["name"], r["id"])
        if r["ring_name"]:
            ring_map.setdefault(r["ring_name"], r["id"])
    names_by_len = sorted((n for n in name_map if len(n) >= 2), key=len, reverse=True)

    def resolve(text, title):
        """경기 표의 선수 칸 → (표시할 이름, 선수 id)"""
        if title and title in title_map:
            return text, title_map[title]
        if text in name_map:
            return text, name_map[text]
        for n in names_by_len:  # '링네임 홍길동' 처럼 섞인 글에서 아는 이름 찾기
            if n in text:
                return n, name_map[n]
        for tok in text.split(" "):
            if tok in ring_map:
                return text, ring_map[tok]
        return text, None

    targets = [e for e in all_events if e["namu_title"]]
    step("⑤ 대회별 대진·결과 가져오는 중", len(targets))
    today = date.today()
    for i, e in enumerate(targets, 1):
        STATUS["done"] = i
        recent = not e["date"] or (today - date.fromisoformat(e["date"])).days < 21
        html = f.namu(e["namu_title"], ttl_hours=6 if recent else 24 * 30)
        if not html:
            continue
        fights = parse_namu_event(html)
        db.execute("DELETE FROM event_fights WHERE event_id=?", (e["id"],))
        for n, x in enumerate(fights, 1):
            a_name, a_id = resolve(x["a"], x["a_title"])
            b_name, b_id = resolve(x["b"], x["b_title"])
            winner = x["winner"]
            if winner and winner not in (a_name, b_name):
                if winner in x["a"]:
                    winner = a_name
                elif winner in x["b"]:
                    winner = b_name
            if not winner and x["result"]:  # 결과 글이 선수 이름으로 시작하면 승자
                for nm in (a_name, b_name):
                    if x["result"].startswith(nm):
                        winner = nm
            db.execute(FIGHT_INSERT, (e["id"], n, x["bout_label"], a_name, a_id, b_name, b_id,
                                      x["result"], winner, x.get("score", "")))
        if i % 5 == 0:
            db.commit()

    step("⑤-2 대항전 가져오는 중")
    html = f.namu("블랙컴뱃", ttl_hours=12)
    versus = parse_versus(html) if html else []
    for v in versus:
        db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?)",
                   (v["id"], v["name"], v["date"], v["venue"], v["main_event"], "대항전", None, "namu"))
        db.execute("DELETE FROM event_fights WHERE event_id=?", (v["id"],))
        for n, x in enumerate(v["fights"], 1):
            a_name, a_id = resolve(x["a"], None)
            b_name, b_id = resolve(x["b"], None)
            winner = a_name if x["winner"] == x["a"] else b_name if x["winner"] == x["b"] else ""
            result = x["result"].replace(x["winner"], winner, 1) if x["winner"] and winner else x["result"]
            db.execute(FIGHT_INSERT, (v["id"], n, x["bout_label"], a_name, a_id, b_name, b_id,
                                      result, winner, ""))
    log(f"  대항전 {len(versus)}개")

    step("⑤-3 챔피언스 리그 가져오는 중")
    cl_total = 0
    for season in CL_SEASONS:
        html = f.namu(f"블랙컴뱃/챔피언스 리그/{season}시즌", ttl_hours=24)
        rounds = parse_cl_season(html, season) if html else []
        if not rounds:
            continue
        # 연도별 목록에 있던 '시즌 묶음' 항목은 회차별 항목으로 대체
        db.execute("DELETE FROM events WHERE source='namu' AND series='챔피언스 리그' AND id NOT LIKE 'CL:%'")
        for v in rounds:
            db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?)",
                       (v["id"], v["name"], v["date"], v["venue"], v["main_event"], "챔피언스 리그",
                        f"블랙컴뱃/챔피언스 리그/{season}시즌", "namu"))
            db.execute("DELETE FROM event_fights WHERE event_id=?", (v["id"],))
            for n, x in enumerate(v["fights"], 1):
                a_name, a_id = resolve(x["a"], None)
                b_name, b_id = resolve(x["b"], None)
                winner = a_name if x["winner"] == x["a"] else b_name if x["winner"] == x["b"] else ""
                result = x["result"]
                if winner and result:  # 결과 글 맨 앞 이름을 정리된 이름으로
                    result = winner.split(" ")[0] if not (a_id or b_id) else winner
                    result = result + " " + x["result"].split(" ", 1)[1] if " " in x["result"] else x["result"]
                db.execute(FIGHT_INSERT, (v["id"], n, x["bout_label"], a_name, a_id, b_name, b_id,
                                          result, winner, ""))
        cl_total += len(rounds)
    log(f"  챔피언스 리그 {cl_total}회차")


# ---------------------------------------------------------------- 뉴스

NEWS_QUERIES = [
    "블랙컴뱃",
    "블랙컴뱃 기자회견 OR 미디어데이 OR 신경전 OR 도발",
    "블랙컴뱃 합류 OR 영입 OR 계약 OR 참전 OR 출전 확정",
]
YOUTUBE_CHANNEL = "UC_IYjiKaQBPq-K1Iq4sM-UQ"  # 블랙컴뱃 공식 유튜브
NEWS_CATEGORIES = [  # 위에서부터 먼저 맞는 분류
    ("도발·신경전", ["도발", "신경전", "저격", "설전", "디스", "독설", "도전장", "패싸움", "충돌", "선넘", "선 넘",
                 "분노", "폭발", "경고", "일침", "참교육", "건방", "시비", "욕설", "멱살", "눈싸움", "vs"]),
    ("기자회견", ["기자회견", "미디어데이", "미디어 데이", "계체량", "페이스오프", "인터뷰", "임베디드", "공식 발표",
              "일문일답"]),
    ("영입·계약", ["영입", "계약", "합류", "이적", "블랙리스트", "블랙 테이블", "오퍼", "입성", "참전", "출전 확정",
               "전격", "깜짝 등장", "상륙"]),
    ("경기 결과", ["승리", "결과", "KO", "서브미션", "판정", "제압", "꺾", "등극", "방어", "격파", "잠재", "완승",
               "역전", "패배", "판정패", "종지부"]),
    ("이슈·논란", ["논란", "약물", "도핑", "징계", "부상", "취소", "사과", "은퇴 선언", "기부", "사건", "반칙", "무효",
               "결장", "해명", "폭로", "고소", "벌금", "매진", "신기록"]),
]


def news_category(title):
    t = title or ""
    for cat, words in NEWS_CATEGORIES:
        for w in words:
            if w == "vs":
                if re.search(r"\bvs\.?\b", t, re.I) and re.search(r"(트레일러|Trailer|신경전|자존심)", t, re.I):
                    return cat
            elif w.lower() in t.lower():
                return cat
    return "일반"


def update_news(db, f):
    import email.utils
    import xml.etree.ElementTree as ET
    step("⑦ 블컴 뉴스·영상 가져오는 중")
    items = []
    for q in NEWS_QUERIES:
        xml = f.get("https://news.google.com/rss/search?q=" + quote(q) + "&hl=ko&gl=KR&ceid=KR:ko", ttl_hours=1)
        if not xml:
            continue
        try:
            root = ET.fromstring(xml.encode("utf-8"))
        except ET.ParseError:
            continue
        for it in root.findall(".//item"):
            title = it.findtext("title") or ""
            source = it.findtext("source") or ""
            if source and title.endswith(" - " + source):
                title = title[: -len(source) - 3]
            try:
                pub = email.utils.parsedate_to_datetime(it.findtext("pubDate")).astimezone().isoformat(timespec="minutes")
            except (TypeError, ValueError):
                pub = ""
            link = it.findtext("link") or ""
            if "블랙컴뱃" not in title and "블컴" not in title and "black combat" not in title.lower():
                continue  # 블랙컴뱃과 관계없는 기사 제외
            items.append(("article", title, source, link, pub, None))
    xml = f.get("https://www.youtube.com/feeds/videos.xml?channel_id=" + YOUTUBE_CHANNEL, ttl_hours=1)
    if xml:
        ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
        try:
            for e in ET.fromstring(xml.encode("utf-8")).findall("a:entry", ns):
                vid = e.findtext("yt:videoId", namespaces=ns) or ""
                try:  # 유튜브 시간(UTC)을 한국 시간으로
                    pub = datetime.fromisoformat(e.findtext("a:published", namespaces=ns)) \
                        .astimezone().isoformat(timespec="minutes")
                except (TypeError, ValueError):
                    pub = ""
                items.append(("video", e.findtext("a:title", namespaces=ns) or "", "블랙컴뱃 유튜브",
                              "https://www.youtube.com/watch?v=" + vid, pub,
                              f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None))
        except ET.ParseError:
            pass
    seen_titles = set()
    n = 0
    db.execute("DELETE FROM news WHERE published NOT LIKE '%+%'")  # 예전 형식(시간대 없음) 정리
    for kind, title, source, url, pub, thumb in items:
        key = norm(title)[:40]
        if not url or key in seen_titles:
            continue
        seen_titles.add(key)
        db.execute("INSERT OR REPLACE INTO news VALUES (?,?,?,?,?,?,?,?)",
                   (url, kind, title, source, url, pub, news_category(title), thumb))
        n += 1
    # 오래된 뉴스 정리 (최근 400개만 보관)
    db.execute("DELETE FROM news WHERE id NOT IN (SELECT id FROM news ORDER BY published DESC LIMIT 400)")
    log(f"  뉴스·영상 {n}건")


SIGNING_PAGES = [("블랙 테이블", "블랙 테이블"), ("블랙리스트", "블랙리스트(블랙컴뱃)")]


def update_signings(db, f):
    step("⑥ 선수 영입(블랙 테이블·블랙리스트) 가져오는 중")
    fighters = db.execute("SELECT id, name, ring_name FROM fighters").fetchall()
    by_name = {norm(r["name"]): r["id"] for r in fighters if r["name"]}
    by_ring = {norm(r["ring_name"]): r["id"] for r in fighters if r["ring_name"]}
    for program, title in SIGNING_PAGES:
        html = f.namu(title, ttl_hours=12)
        items = parse_black_table(html) if html else []
        if not items:
            log(f"  ! {program} 정보를 찾지 못했습니다 (기존 데이터 유지)")
            continue
        db.execute("DELETE FROM signings WHERE source='namu' AND program=?", (program,))
        for x in items:
            base = re.sub(r"\s*\(.*?\)", "", x["name"]).strip()  # '난딘에르덴 (김인성)' → 앞부분
            fid = (by_name.get(norm(x["name"])) or by_name.get(norm(base))
                   or by_ring.get(norm(x["ring_name"])))
            db.execute("INSERT INTO signings(year,name,ring_name,fighter_id,program,description,source) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (x["year"], x["name"], x["ring_name"], fid, program, x["description"], "namu"))
        log(f"  {program}: {len(items)}명")


if __name__ == "__main__":
    import sys
    m = sys.argv[1] if len(sys.argv) > 1 else "quick"
    STATUS["running"] = True
    _run(m)

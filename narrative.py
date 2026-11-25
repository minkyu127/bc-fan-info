"""두 선수 사이의 서사(맞대결 이야기)를 모아둔 데이터로 만들어 냄.

재료: 랭킹·타이틀, 영입 경로(블랙리스트/블랙 테이블), 국적·소속팀, 경기 전 흐름(연승·연패),
     파이팅 스타일, 과거 맞대결(재대결·복수전), 공통 상대, 나무위키 이야기 속 신경전 문장, 관련 기사.
"""
import re

STYLE_WORDS = [
    ("그래플러", ["그래플러", "그래플링", "레슬러", "레슬링", "주짓수", "삼보", "유도", "서브미션", "테이크다운", "초크"]),
    ("타격가", ["타격가", "스트라이커", "킥복싱", "무에타이", "복싱", "카운터", "타격", "펀치", "KO"]),
]
HEAT_WORDS = ["콜아웃", "도발", "신경전", "설전", "욕", "지목", "복수", "악연", "저격", "디스", "도전장",
              "경고", "자존심", "충돌", "패싸움", "멱살", "눈싸움", "선전포고", "리벤지", "앙숙"]


def josa(word, pair):
    """받침에 따라 조사 선택: josa('김재웅', ('이', '가'))"""
    if not word:
        return pair[1]
    ch = word[-1]
    if "가" <= ch <= "힣":
        return pair[0] if (ord(ch) - 0xAC00) % 28 else pair[1]
    return pair[1]


def short(name):
    return (name or "").strip()


def style_of(f):
    text = (f or {}).get("summary") or ""
    best, score = None, 0
    for label, words in STYLE_WORDS:
        n = sum(text.count(w) for w in words)
        if n > score:
            best, score = label, n
    return best


def record_before(bc, date):
    """이 경기 전까지의 블랙컴뱃 성적: (승, 패, 연승/연패 글, 직전 경기)"""
    prev = sorted([b for b in bc if b.get("date") and date and b["date"] < date], key=lambda b: b["date"])
    w = sum(1 for b in prev if b["outcome"] == "W")
    l = sum(1 for b in prev if b["outcome"] == "L")
    streak = ""
    seq = [b["outcome"] for b in prev if b["outcome"] in ("W", "L")]
    if seq:
        last, n = seq[-1], 0
        for o in reversed(seq):
            if o != last:
                break
            n += 1
        if n >= 2:
            streak = f"{n}연승" if last == "W" else f"{n}연패"
    return w, l, streak, (prev[-1] if prev else None), prev


def after(bc, date):
    nxt = sorted([b for b in bc if b.get("date") and date and b["date"] > date], key=lambda b: b["date"])
    return nxt[0] if nxt else None


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?다])\s+", text or "") if len(s.strip()) > 8]


def build(x, e, sides, h2h, signings, news, stories, country_name=lambda c: c):
    """경기 하나의 맞대결 서사 → [{title, text}]"""
    A, B = sides
    an, bn = short(A["name"]), short(B["name"])
    fa, fb = A.get("f") or {}, B.get("f") or {}
    ar = fa.get("ring_name")
    br = fb.get("ring_name")
    a_call = f"'{ar}' {an}" if ar else an
    b_call = f"'{br}' {bn}" if br else bn
    date = e.get("date")
    label = x.get("bout_label") or ""
    out = []

    # 1. 맞대결 구도 (타이틀, 랭킹)
    lines = []
    a_rank, b_rank = (A.get("rank") or (None, None))[1], (B.get("rank") or (None, None))[1]
    if "타이틀" in label:
        m = re.search(r"(\d+)차 방어전", label)
        if m:
            lines.append(f"{e.get('name')}에서 열린 {label.split('(')[0].strip()}. 챔피언의 {m.group(1)}차 방어전으로, "
                         f"벨트를 지키려는 쪽과 빼앗으려는 쪽의 정면 승부였다.")
        else:
            lines.append(f"{e.get('name')}의 {label.split('(')[0].strip()}. 블랙컴뱃 벨트가 걸린 경기였다.")
    elif "메인" in label or "MAIN" in label.upper():
        lines.append(f"{e.get('name')}의 메인 이벤트를 장식한 경기.")
    if a_rank is not None and b_rank is not None:
        if 0 in (a_rank, b_rank):
            champ, chal = (a_call, (b_call, b_rank)) if a_rank == 0 else (b_call, (a_call, a_rank))
            lines.append(f"현재 랭킹 기준으로는 챔피언 {champ}{josa(champ, ('과', '와'))} 랭킹 {chal[1]}위 {chal[0]}의 구도다.")
        else:
            gap = abs(a_rank - b_rank)
            if gap <= 2:
                lines.append(f"현재 랭킹 {a_rank}위와 {b_rank}위, 순위가 바로 맞닿은 두 선수의 대결로 "
                             f"이 경기 결과가 서열을 정리하는 의미가 있었다.")
            else:
                hi, lo = (a_call, b_call) if a_rank < b_rank else (b_call, a_call)
                lines.append(f"현재 랭킹은 {hi}{josa(hi, ('이', '가'))} {min(a_rank, b_rank)}위, "
                             f"{lo}{josa(lo, ('이', '가'))} {max(a_rank, b_rank)}위. 상위 랭커를 잡아 단숨에 치고 올라가려는 "
                             f"{lo}에게는 놓칠 수 없는 기회였다.")
    if lines:
        out.append({"title": "맞대결 구도", "text": " ".join(lines)})

    # 2. 출신·영입 경로·국적 대비
    lines = []
    sa, sb = signings.get(A.get("id")), signings.get(B.get("id"))
    if sa or sb:
        def how(s, who):
            if not s:
                return None
            if s["program"] == "블랙리스트":
                return f"{who}{josa(who, ('은', '는'))} 블랙컴뱃이 직접 지목한 국내 강자 명단 '블랙리스트'({s['year']}) 출신"
            if s["program"] == "블랙 테이블":
                return f"{who}{josa(who, ('은', '는'))} 해외 강자 영입 프로그램 '블랙 테이블'({s['year']}년)로 합류한 선수"
            return f"{who}{josa(who, ('은', '는'))} {s['program']}로 합류한 선수"
        if sa and sb and sa["program"] == sb["program"] == "블랙리스트":
            lines.append(f"두 선수 모두 블랙컴뱃이 직접 지목한 국내 강자 명단 '블랙리스트' 출신이다 "
                         f"({an} {sa['year']}, {bn} {sb['year']}). 블랙컴뱃이 '데려와야 할 선수'로 꼽은 이름끼리의 충돌이다.")
        elif sa and sb and sa["program"] == sb["program"]:
            lines.append(f"두 선수 모두 '{sa['program']}'로 블랙컴뱃에 합류한 선수들이다 ({an} {sa['year']}, {bn} {sb['year']}).")
        else:
            parts = [p for p in (how(sa, a_call), how(sb, b_call)) if p]
            lines.append(", ".join(parts) + ".")
            if sa and sb:
                lines.append("서로 다른 길로 블랙컴뱃에 들어온 두 선수가 같은 케이지에서 만난 셈이다.")
    ca, cb = fa.get("country"), fb.get("country")
    if ca and cb and ca != cb:
        lines.append(f"{country_name(ca)}의 {an}{josa(an, ('과', '와'))} {country_name(cb)}의 {bn}, "
                     f"국가의 자존심도 함께 걸린 매치업이었다.")
    if e.get("series") == "블랙컵":
        lines.append(f"국가대항전 '블랙컵'의 한 경기로, 개인 승패가 팀 점수로 이어지는 무게감이 있었다.")
    if e.get("series") == "챔피언스 리그" and e.get("main_event"):
        lines.append(f"팀 리그전인 챔피언스 리그 경기로, 팀 대결({e['main_event']})의 한 축을 맡았다.")
    if e.get("series") == "대항전":
        lines.append(f"단체 간 자존심이 걸린 '{e.get('name')}'에서 블랙컴뱃을 대표해 싸운 경기였다.")
    ta, tb = fa.get("team"), fb.get("team")
    if ta and tb and ta == tb:
        lines.append(f"놀랍게도 두 선수는 같은 소속팀({ta}) 출신이다.")
    if lines:
        out.append({"title": "서로 다른 출발점", "text": " ".join(lines)})

    # 3. 경기 전 흐름과 스타일
    lines = []
    pa = record_before((A.get("bundle") or {}).get("bc", []), date)
    pb = record_before((B.get("bundle") or {}).get("bc", []), date)
    for who, (w, l, streak, last, prev) in ((a_call, pa), (b_call, pb)):
        if not prev:
            if A.get("bundle") or B.get("bundle"):
                lines.append(f"{who}에게는 이 경기가 블랙컴뱃에서의 첫 무대(수집된 기록 기준)였다.")
            continue
        t = f"{who}{josa(who, ('은', '는'))} 이 경기 전까지 블랙컴뱃에서 {w}승 {l}패"
        t += f", {streak} 중이었다" if streak else ("였다" if l else "였다")
        if last:
            res = "승리" if last["outcome"] == "W" else "패배" if last["outcome"] == "L" else "결과를 남겼"
            t = t[:-2] + "었고" if t.endswith("이었다") else t[:-1] + "고"
            t += f", 직전 경기에서는 {last['opp']}에게 {res}했다"
        lines.append(t + ".")
    st_a, st_b = style_of(fa), style_of(fb)
    if st_a and st_b and st_a != st_b:
        lines.append(f"스타일도 정반대다. {an}{josa(an, ('은', '는'))} {st_a}, {bn}{josa(bn, ('은', '는'))} {st_b}에 가까워 "
                     f"'{st_a} vs {st_b}'라는 고전적인 대결 구도가 만들어졌다.")
    elif st_a and st_a == st_b:
        lines.append(f"둘 다 {st_a} 성향이라 서로의 장기를 정면으로 맞부딪치는 싸움이 예상됐다.")
    # 공통 상대
    if pa[4] and pb[4]:
        oa = {b["opp"]: b for b in pa[4]}
        common = [(o, oa[o], b) for o, b in ((b["opp"], b) for b in pb[4]) if o in oa]
        for o, ra, rb in common[:2]:
            oa_, ob_ = ra["outcome"], rb["outcome"]
            if oa_ != ob_ and oa_ in ("W", "L") and ob_ in ("W", "L"):
                win_side, lose_side = (an, bn) if oa_ == "W" else (bn, an)
                lines.append(f"공통 상대 {o}에게 {win_side}{josa(win_side, ('은', '는'))} 이겼고 "
                             f"{lose_side}{josa(lose_side, ('은', '는'))} 졌다. 이 엇갈린 결과가 맞대결의 예고편이 됐다.")
            elif oa_ == ob_ == "W":
                lines.append(f"두 선수 모두 {o}{josa(o, ('을', '를'))} 꺾은 적이 있다. 같은 상대를 넘은 두 사람 중 "
                             f"누가 더 강한지 가리는 경기이기도 했다.")
            elif oa_ == ob_ == "L":
                lines.append(f"두 선수 모두 {o}에게 패한 아픔이 있다. 패배를 딛고 올라설 쪽이 누구인지가 관심사였다.")
    if lines:
        out.append({"title": "경기 전 흐름", "text": " ".join(lines)})

    # 4. 악연: 재대결 / 신경전
    lines = []
    earlier = [r for r in h2h if (r["event"].get("date") or "") < (date or "") and r.get("result_text")]
    if earlier:
        r = earlier[-1]
        wn = r.get("winner") or ""
        loser = an if wn == bn or (wn and wn in bn) else bn if wn else ""
        lines.append(f"두 선수는 이미 {r['event'].get('name') or r['event_id']}에서 한 번 맞붙었다 ({r['result_text']}).")
        if loser:
            lines.append(f"이번 경기는 {loser}에게 복수전이자, 상대에게는 승리를 증명할 재대결이었다.")
    def mentions(s, name, ring):
        return any(t in s for t in name.split() if len(t) >= 2) or bool(ring and len(ring) >= 2 and ring in s)
    heat = []
    for text, this_fight in stories:
        for s in sentences(text):
            if not any(w in s for w in HEAT_WORDS):
                continue
            # 이 경기 이야기면 그대로, 다른 글이면 두 선수가 모두 나오는 문장만
            if this_fight or (mentions(s, an, ar) and mentions(s, bn, br)):
                q = s[:160] + ("…" if len(s) > 160 else "")
                if q not in heat:
                    heat.append(q)
    if heat:
        lines.append("경기를 둘러싼 신경전도 있었다. “" + "” “".join(heat[:2]) + "” (나무위키)")
    if news:
        lines.append("당시 기사 제목만 봐도 분위기를 알 수 있다: " + " / ".join(f"「{n['title']}」" for n in news[:3]) + ".")
    if lines:
        out.append({"title": "두 선수 사이의 악연과 신경전", "text": " ".join(lines)})

    # 5. 결과와 그 이후
    lines = []
    wn = x.get("winner")
    if x.get("result_text"):
        if wn:
            w_side, l_side = (A, B) if wn == A["name"] else (B, A)
            w_call = a_call if w_side is A else b_call
            l_call = b_call if w_side is A else a_call
            res = x["result_text"]
            how = "판정" if "판정" in res else "KO" if "KO" in res.upper() else "서브미션" if ("SUB" in res.upper() or "서브미션" in res or "초크" in res) else ""
            t = f"결과는 {w_call}의 승리({res})."
            if how == "판정" and x.get("scorecard"):
                t += f" 심판 채점은 {x['scorecard']}."
            if "타이틀" in label:
                if (w_side.get("rank") or (None, None))[1] == 0:
                    t += f" {w_call}{josa(w_call, ('이', '가'))} 벨트의 주인임을 다시 증명했다."
                else:
                    t += f" 이 승리로 {w_call}{josa(w_call, ('이', '가'))} 타이틀을 손에 넣었다."
            lines.append(t)
            nw = after((w_side.get("bundle") or {}).get("bc", []), date)
            nl = after((l_side.get("bundle") or {}).get("bc", []), date)
            if nw:
                lines.append(f"{w_call}{josa(w_call, ('은', '는'))} 이후 {nw['event'].get('name') or nw['event_id']}에서 "
                             f"{nw['opp']}{josa(nw['opp'], ('과', '와'))} 맞붙었고"
                             + (" 승리를 이어갔다." if nw["outcome"] == "W" else " 패배했다." if nw["outcome"] == "L" else " 경기를 앞두고 있다."))
            if nl:
                lines.append(f"{l_call}{josa(l_call, ('은', '는'))} 다음 경기에서 {nl['opp']}{josa(nl['opp'], ('을', '를'))} 상대로 "
                             + ("반등에 성공했다." if nl["outcome"] == "W" else "다시 고개를 숙였다." if nl["outcome"] == "L" else "재기를 노린다."))
        else:
            lines.append(f"결과: {x['result_text']}.")
    else:
        lines.append("아직 치러지지 않은 경기다. 이 서사의 결말은 케이지 안에서 정해진다.")
    out.append({"title": "결말, 그리고 그 이후", "text": " ".join(lines)})
    return out

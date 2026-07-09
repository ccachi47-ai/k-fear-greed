# -*- coding: utf-8 -*-
"""
한국시장 공포·탐욕지수 자동 수집기 (v3 - 네이버 금융, 다중 fallback)
- 로그인 불필요. 실패한 지표는 자동으로 대체 경로 시도.
"""
import ast
import json
import os
import traceback
from datetime import datetime, timedelta

import requests

TODAY = datetime.now()
FMT = "%Y%m%d"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
      "Referer": "https://finance.naver.com"}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def map_range(v, in_lo, in_hi):
    if in_lo == in_hi:
        return 50.0
    t = (v - in_lo) / (in_hi - in_lo)
    return clamp(t * 100.0, 0.0, 100.0)


def safe(fn):
    try:
        return fn()
    except Exception:
        traceback.print_exc()
        return None


# ---------------------------------------------------------------
# 네이버 시세 API (검증됨: KOSPI, KOSDAQ, ETF 종목코드 지원)
# ---------------------------------------------------------------
def naver_closes(symbol, days_back):
    start = (TODAY - timedelta(days=days_back)).strftime(FMT)
    end = TODAY.strftime(FMT)
    url = ("https://api.finance.naver.com/siseJson.naver"
           f"?symbol={symbol}&requestType=1&startTime={start}"
           f"&endTime={end}&timeframe=day")
    r = requests.get(url, headers=UA, timeout=30)
    data = ast.literal_eval(r.text.strip())
    closes = []
    for row in data[1:]:
        if isinstance(row, list) and len(row) >= 5:
            try:
                closes.append(float(row[4]))
            except (TypeError, ValueError):
                continue
    return closes


def naver_index_day_closes(code, pages=12):
    """네이버 지수 일별시세 페이지 스크래핑 (VKOSPI 등 siseJson 미지원 지수용)"""
    import pandas as pd
    closes = []
    for page in range(1, pages + 1):
        url = f"https://finance.naver.com/sise/sise_index_day.naver?code={code}&page={page}"
        r = requests.get(url, headers=UA, timeout=30)
        r.encoding = "euc-kr"
        for df in pd.read_html(r.text):
            if "체결가" in [str(c) for c in df.columns]:
                for v in df["체결가"].dropna().tolist():
                    try:
                        closes.append(float(str(v).replace(",", "")))
                    except (TypeError, ValueError):
                        continue
    closes.reverse()  # 페이지는 최신순 -> 오래된순으로 뒤집기
    return closes


# ---------------------------------------------------------------
# 지표 1. 시장 모멘텀: 코스피 vs 125일 이동평균  [자동 - 검증됨]
# ---------------------------------------------------------------
def indicator_momentum():
    closes = naver_closes("KOSPI", 300)
    if len(closes) < 125:
        return None
    ma125 = sum(closes[-125:]) / 125
    cur = closes[-1]
    dev = (cur / ma125 - 1) * 100
    return {"score": map_range(dev, -8, 8),
            "detail": f"코스피 {cur:,.0f} / 125일선 대비 {dev:+.1f}%"}


# ---------------------------------------------------------------
# 지표 2. 주가 강도: 코스피 52주 밴드 내 위치  [자동 - 신고/신저 대체]
# ---------------------------------------------------------------
def indicator_strength():
    closes = naver_closes("KOSPI", 380)
    if len(closes) < 200:
        return None
    hi, lo, cur = max(closes), min(closes), closes[-1]
    if hi == lo:
        return None
    pos = (cur - lo) / (hi - lo) * 100
    return {"score": pos,
            "detail": f"52주 밴드 내 위치 {pos:.0f}% (저점 {lo:,.0f}~고점 {hi:,.0f})"}


# ---------------------------------------------------------------
# 지표 3. 시장 폭: 코스피+코스닥 20일 상승일 비율  [자동 - ADR 대체]
# ---------------------------------------------------------------
def indicator_breadth():
    ups, total = 0, 0
    for sym in ["KOSPI", "KOSDAQ"]:
        closes = naver_closes(sym, 60)
        if len(closes) < 21:
            continue
        for i in range(-20, 0):
            total += 1
            if closes[i] > closes[i - 1]:
                ups += 1
    if total == 0:
        return None
    ratio = ups / total * 100
    return {"score": map_range(ratio, 30, 70),
            "detail": f"최근 20일 상승일 비율 {ratio:.0f}% (코스피+코스닥)"}


# ---------------------------------------------------------------
# 지표 5. 변동성: VKOSPI vs 50일 평균, 실패 시 실현변동성으로 대체
# ---------------------------------------------------------------
def indicator_volatility():
    closes = []
    try:
        closes = naver_closes("VKOSPI", 120)
    except Exception:
        pass
    if len(closes) < 50:
        try:
            closes = naver_index_day_closes("VKOSPI")
        except Exception:
            closes = []
    if len(closes) >= 50:
        ma50 = sum(closes[-50:]) / 50
        cur = closes[-1]
        dev = (cur / ma50 - 1) * 100
        return {"score": map_range(dev, 45, -30),
                "detail": f"VKOSPI {cur:.1f} / 50일 평균 대비 {dev:+.0f}%"}

    # 최후 fallback: 코스피 실현변동성 (20일 vs 100일)
    k = naver_closes("KOSPI", 200)
    if len(k) < 110:
        return None
    rets = [(k[i] / k[i - 1] - 1) for i in range(1, len(k))]

    def vol(xs):
        m = sum(xs) / len(xs)
        return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5

    v20, v100 = vol(rets[-20:]), vol(rets[-100:])
    if v100 == 0:
        return None
    dev = (v20 / v100 - 1) * 100
    return {"score": map_range(dev, 80, -40),
            "detail": f"실현변동성 20일/100일 대비 {dev:+.0f}% (VKOSPI 대체)"}


# ---------------------------------------------------------------
# 지표 6. 안전자산 수요: 코스피 vs 국고채 ETF  [자동 - 검증됨]
# ---------------------------------------------------------------
def indicator_safehaven():
    kospi = naver_closes("KOSPI", 60)
    bond = naver_closes("148070", 60)
    if len(kospi) < 21 or len(bond) < 21:
        return None
    gap = (kospi[-1] / kospi[-21] - 1) * 100 - (bond[-1] / bond[-21] - 1) * 100
    return {"score": map_range(gap, -6, 6),
            "detail": f"주식-채권 20일 수익률차 {gap:+.1f}%p"}


# ---------------------------------------------------------------
# 지표 8. 외국인 수급: 20일 누적 순매수 (2가지 경로 시도)
# ---------------------------------------------------------------
def indicator_foreign():
    import pandas as pd
    values = []
    # 경로 1: 투자자별 매매동향 일별 페이지
    try:
        for page in range(1, 5):
            url = ("https://finance.naver.com/sise/investorDealTrendDay.naver"
                   f"?bizdate={TODAY.strftime(FMT)}&sosok=&page={page}")
            r = requests.get(url, headers=UA, timeout=30)
            r.encoding = "euc-kr"
            for df in pd.read_html(r.text):
                df.columns = ["".join(map(str, c)) if isinstance(c, tuple) else str(c)
                              for c in df.columns]
                fcols = [c for c in df.columns if "외국인" in c]
                if fcols:
                    for v in df[fcols[0]].dropna().tolist():
                        try:
                            values.append(float(str(v).replace(",", "")))
                        except (TypeError, ValueError):
                            continue
            if len(values) >= 20:
                break
    except Exception:
        traceback.print_exc()
    if len(values) >= 20:
        flow_jo = sum(values[:20]) / 10000.0  # 억원 -> 조원
        return {"score": map_range(flow_jo, -4, 4),
                "detail": f"외국인 20일 누적 {flow_jo:+.2f}조원"}
    return None


# ---------------------------------------------------------------
# 지표 7. 크레딧 스프레드: ECOS API (키 있을 때만)
# ---------------------------------------------------------------
def indicator_credit():
    key = os.environ.get("ECOS_KEY", "")
    gov3 = os.environ.get("ECOS_ITEM_GOV3", "")
    bbb3 = os.environ.get("ECOS_ITEM_BBB3", "")
    if not (key and gov3 and bbb3):
        return None

    def latest_rate(item):
        s = (TODAY - timedelta(days=14)).strftime(FMT)
        e = TODAY.strftime(FMT)
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/50/"
               f"721Y001/D/{s}/{e}/{item}")
        data = requests.get(url, timeout=30).json()
        return float(data["StatisticSearch"]["row"][-1]["DATA_VALUE"])

    spread = latest_rate(bbb3) - latest_rate(gov3)
    return {"score": map_range(spread, 8.0, 6.0),
            "detail": f"BBB- 스프레드 {spread:.2f}%p"}


# ---------------------------------------------------------------
# 수동 지표: overrides.json (풋/콜)
# ---------------------------------------------------------------
def load_overrides():
    try:
        with open("overrides.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def indicator_putcall(ov):
    v = ov.get("pcr")
    if v is None:
        return None
    return {"score": map_range(float(v), 1.4, 0.6),
            "detail": f"풋/콜 {float(v):.2f} (수동)"}


# ---------------------------------------------------------------
# 메인
# ---------------------------------------------------------------
def main():
    ov = load_overrides()
    indicators = {
        "momentum":  {"name": "시장 모멘텀 (코스피 vs 125일선)",   "res": safe(indicator_momentum)},
        "strength":  {"name": "주가 강도 (52주 밴드 내 위치)",     "res": safe(indicator_strength)},
        "breadth":   {"name": "시장 폭 (20일 상승일 비율)",        "res": safe(indicator_breadth)},
        "putcall":   {"name": "풋/콜 비율 (K200 옵션·수동)",       "res": indicator_putcall(ov)},
        "vol":       {"name": "변동성 (VKOSPI/실현변동성)",        "res": safe(indicator_volatility)},
        "safehaven": {"name": "안전자산 수요 (주식 vs 국채)",       "res": safe(indicator_safehaven)},
        "credit":    {"name": "크레딧 스프레드 (BBB- - 국고3년)",   "res": safe(indicator_credit)},
        "foreign":   {"name": "외국인 수급 (20일 누적)",            "res": safe(indicator_foreign)},
    }

    scores = [v["res"]["score"] for v in indicators.values() if v["res"]]
    composite = round(sum(scores) / len(scores)) if scores else None

    os.makedirs("docs", exist_ok=True)
    path = "docs/data.json"
    history = []
    try:
        with open(path, encoding="utf-8") as f:
            history = json.load(f).get("history", [])
    except Exception:
        pass

    today_str = TODAY.strftime("%Y-%m-%d")
    if composite is not None:
        history = [h for h in history if h["date"] != today_str]
        history.append({"date": today_str, "score": composite})
        history = history[-60:]

    out = {
        "updated": TODAY.strftime("%Y-%m-%d %H:%M"),
        "score": composite,
        "label": None,
        "indicators": {
            k: {"name": v["name"],
                "score": round(v["res"]["score"]) if v["res"] else None,
                "detail": v["res"]["detail"] if v["res"] else None}
            for k, v in indicators.items()
        },
        "history": history,
    }
    if composite is not None:
        labels = [(25, "극단적 공포"), (45, "공포"), (55, "중립"), (75, "탐욕"), (101, "극단적 탐욕")]
        out["label"] = next(l for th, l in labels if composite < th)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print(f"완료: 지수 {composite} ({len(scores)}/8개 지표)")


if __name__ == "__main__":
    main()

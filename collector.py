# -*- coding: utf-8 -*-
"""
한국시장 공포·탐욕지수 수집기 v4
- 네이버 금융 (PC/모바일 API 다중 경로), 로그인 불필요
- 지표별 fallback + 상세 로그
"""
import ast
import json
import os
import traceback
from datetime import datetime, timedelta

import requests

TODAY = datetime.now()
FMT = "%Y%m%d"
UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
      "Referer": "https://m.stock.naver.com"}


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def map_range(v, in_lo, in_hi):
    if in_lo == in_hi:
        return 50.0
    t = (v - in_lo) / (in_hi - in_lo)
    return clamp(t * 100.0, 0.0, 100.0)


def safe(name, fn):
    try:
        r = fn()
        print(f"[OK] {name}: {r['detail'] if r else '데이터 없음'}")
        return r
    except Exception:
        print(f"[FAIL] {name}")
        traceback.print_exc()
        return None


# ---------------------------------------------------------------
# 시세 수집 유틸
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


def guess_to_jo(values):
    """수급 금액 리스트의 단위를 추정해 조원으로 환산 (원/백만원/억원 자동감지)"""
    mx = max(abs(v) for v in values) if values else 0
    if mx > 1e11:      # 원 단위
        return [v / 1e12 for v in values]
    if mx > 1e6:       # 백만원 단위
        return [v / 1e6 for v in values]
    return [v / 1e4 for v in values]  # 억원 단위


# ---------------------------------------------------------------
# 지표 1. 시장 모멘텀
# ---------------------------------------------------------------
def indicator_momentum():
    closes = naver_closes("KOSPI", 300)
    if len(closes) < 125:
        return None
    ma125 = sum(closes[-125:]) / 125
    cur = closes[-1]
    dev = (cur / ma125 - 1) * 100
    return {"score": map_range(dev, -8, 8),
            "detail": f"코스피 {cur:,.0f} · 125일선 대비 {dev:+.1f}%"}


# ---------------------------------------------------------------
# 지표 2. 주가 강도 (52주 밴드 내 위치)
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
            "detail": f"52주 밴드 내 위치 {pos:.0f}% ({lo:,.0f}~{hi:,.0f})"}


# ---------------------------------------------------------------
# 지표 3. 시장 폭 (20일 상승일 비율, 코스피+코스닥)
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
            "detail": f"최근 20일 상승일 비율 {ratio:.0f}%"}


# ---------------------------------------------------------------
# 지표 5. 변동성 (VKOSPI 2경로 → 실현변동성 fallback)
# ---------------------------------------------------------------
def indicator_volatility():
    closes = []
    for fn in [
        lambda: naver_closes("VKOSPI", 120),
        lambda: _mobile_index_closes("VKOSPI", 120),
    ]:
        try:
            closes = fn()
            if len(closes) >= 50:
                break
        except Exception:
            continue
    if len(closes) >= 50:
        ma50 = sum(closes[-50:]) / 50
        cur = closes[-1]
        dev = (cur / ma50 - 1) * 100
        return {"score": map_range(dev, 45, -30),
                "detail": f"VKOSPI {cur:.1f} · 50일 평균 대비 {dev:+.0f}%"}

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
            "detail": f"실현변동성 20일/100일 {dev:+.0f}% (VKOSPI 대체)"}


def _mobile_index_closes(code, days_back):
    url = f"https://m.stock.naver.com/api/index/{code}/price?pageSize=200&page=1"
    data = requests.get(url, headers=UA, timeout=30).json()
    closes = []
    for row in reversed(data):
        v = row.get("closePrice") or row.get("clpr")
        if v is not None:
            closes.append(float(str(v).replace(",", "")))
    return closes


# ---------------------------------------------------------------
# 지표 6. 안전자산 수요
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
# 지표 8. 외국인 수급 (3중 경로)
# ---------------------------------------------------------------
def indicator_foreign():
    # 경로 A: 모바일 JSON API (투자자별 매매동향)
    for url in [
        "https://m.stock.naver.com/api/index/KOSPI/trend?pageSize=30&page=1",
        "https://m.stock.naver.com/api/stocks/trend/index/KOSPI?pageSize=30",
    ]:
        try:
            data = requests.get(url, headers=UA, timeout=30).json()
            rows = data if isinstance(data, list) else data.get("result") or data.get("trends") or []
            vals = []
            for row in rows:
                for key in ["foreignValue", "foreignerPureBuyQuant", "frgn", "foreign"]:
                    if key in row and row[key] is not None:
                        try:
                            vals.append(float(str(row[key]).replace(",", "")))
                        except (TypeError, ValueError):
                            pass
                        break
            if len(vals) >= 20:
                jo = guess_to_jo(vals[:20])
                flow = sum(jo)
                print(f"  외국인 경로A 성공: {url}")
                return {"score": map_range(flow, -4, 4),
                        "detail": f"외국인 20일 누적 {flow:+.2f}조원"}
        except Exception:
            continue

    # 경로 B: PC 일별 매매동향 HTML
    try:
        import pandas as pd
        values = []
        for page in range(1, 5):
            url = ("https://finance.naver.com/sise/investorDealTrendDay.naver"
                   f"?bizdate={TODAY.strftime(FMT)}&sosok=&page={page}")
            r = requests.get(url, headers={**UA, "Referer": "https://finance.naver.com"},
                             timeout=30)
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
        if len(values) >= 20:
            jo = guess_to_jo(values[:20])
            flow = sum(jo)
            print("  외국인 경로B 성공")
            return {"score": map_range(flow, -4, 4),
                    "detail": f"외국인 20일 누적 {flow:+.2f}조원"}
    except Exception:
        traceback.print_exc()
    return None


# ---------------------------------------------------------------
# 지표 7. 크레딧 스프레드 (ECOS)
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
# 수동 지표 (overrides.json)
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
            "detail": f"풋/콜 {float(v):.2f} (수동 입력)"}


# ---------------------------------------------------------------
# 메인
# ---------------------------------------------------------------
def main():
    ov = load_overrides()
    indicators = {
        "momentum":  {"name": "시장 모멘텀", "sub": "코스피 vs 125일 이동평균",
                      "res": safe("모멘텀", indicator_momentum)},
        "strength":  {"name": "주가 강도", "sub": "코스피 52주 밴드 내 위치",
                      "res": safe("주가강도", indicator_strength)},
        "breadth":   {"name": "시장 폭", "sub": "20일 상승일 비율 (코스피+코스닥)",
                      "res": safe("시장폭", indicator_breadth)},
        "putcall":   {"name": "풋/콜 비율", "sub": "K200 옵션 (수동 입력)",
                      "res": indicator_putcall(ov)},
        "vol":       {"name": "시장 변동성", "sub": "VKOSPI 또는 실현변동성",
                      "res": safe("변동성", indicator_volatility)},
        "safehaven": {"name": "안전자산 수요", "sub": "주식 vs 국고채 20일 수익률차",
                      "res": safe("안전자산", indicator_safehaven)},
        "credit":    {"name": "크레딧 스프레드", "sub": "BBB- 회사채 - 국고채 3년",
                      "res": safe("크레딧", indicator_credit)},
        "foreign":   {"name": "외국인 수급", "sub": "코스피 20일 누적 순매수",
                      "res": safe("외국인", indicator_foreign)},
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
        history = history[-90:]

    out = {
        "updated": TODAY.strftime("%Y-%m-%d %H:%M"),
        "score": composite,
        "label": None,
        "indicators": {
            k: {"name": v["name"], "sub": v["sub"],
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

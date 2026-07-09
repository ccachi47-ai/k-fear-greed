# -*- coding: utf-8 -*-
"""
한국시장 공포·탐욕지수 자동 수집기
- pykrx로 KRX 데이터를 받아 8개 지표 중 자동화 가능한 6개를 계산
- 나머지(풋/콜, 신고/신저, 크레딧 스프레드)는 overrides.json 수동값 또는 ECOS API 사용
- 결과를 docs/data.json 에 저장 (GitHub Pages가 docs 폴더를 서빙)

실행: python collector.py
필요 환경변수:
  KRX_ID, KRX_PW  : KRX 정보데이터시스템 계정 (data.krx.co.kr 무료 가입)
  ECOS_KEY        : (선택) 한국은행 ECOS API 키 - 크레딧 스프레드 자동화용
"""
import json
import os
import traceback
from datetime import datetime, timedelta

from pykrx import stock

# pykrx 라이브러리 내부 버그 우회: 지수 이름 조회 실패 시 무시하도록 패치
import pykrx.stock.stock_api as _stock_api
_orig_get_index_ticker_name = _stock_api.get_index_ticker_name
def _safe_get_index_ticker_name(ticker):
    try:
        return _orig_get_index_ticker_name(ticker)
    except Exception:
        return ""
_stock_api.get_index_ticker_name = _safe_get_index_ticker_name

TODAY = datetime.now()
FMT = "%Y%m%d"


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def map_range(v, in_lo, in_hi):
    """in_lo -> 0점, in_hi -> 100점으로 선형 변환 (역방향도 지원)"""
    if in_lo == in_hi:
        return 50.0
    t = (v - in_lo) / (in_hi - in_lo)
    return clamp(t * 100.0, 0.0, 100.0)


def date_range(days_back):
    start = (TODAY - timedelta(days=days_back)).strftime(FMT)
    end = TODAY.strftime(FMT)
    return start, end


def safe(fn):
    """지표 하나가 실패해도 전체가 죽지 않도록 감싸기"""
    try:
        return fn()
    except Exception:
        traceback.print_exc()
        return None


# ---------------------------------------------------------------
# 지표 1. 시장 모멘텀: 코스피 vs 125일 이동평균
# ---------------------------------------------------------------
def indicator_momentum():
    start, end = date_range(300)
    df = stock.get_index_ohlcv_by_date(start, end, "1001")  # 1001 = 코스피
    close = df["종가"]
    if len(close) < 125:
        return None
    ma125 = close.tail(125).mean()
    cur = float(close.iloc[-1])
    dev = (cur / ma125 - 1) * 100
    return {
        "score": map_range(dev, -8, 8),
        "detail": f"코스피 {cur:,.0f} / 125일선 대비 {dev:+.1f}%",
        "raw": {"kospi": cur, "ma125": round(float(ma125), 2)},
    }


# ---------------------------------------------------------------
# 지표 3. 시장 폭: ADR (20일 상승종목수 합 / 하락종목수 합)
# ---------------------------------------------------------------
def indicator_breadth():
    start, end = date_range(45)
    df = stock.get_index_ohlcv_by_date(start, end, "1001")
    dates = df.index[-20:]
    ups, downs = 0, 0
    for d in dates:
        day = d.strftime(FMT)
        snap = stock.get_market_ohlcv_by_ticker(day, market="KOSPI")
        ups += int((snap["등락률"] > 0).sum())
        downs += int((snap["등락률"] < 0).sum())
    if downs == 0:
        return None
    adr = ups / downs * 100
    return {
        "score": map_range(adr, 70, 130),
        "detail": f"ADR {adr:.1f}%",
        "raw": {"adr": round(adr, 1)},
    }


# ---------------------------------------------------------------
# 지표 5. 변동성: VKOSPI vs 50일 평균
# 지수 코드가 버전에 따라 다를 수 있어 이름으로 탐색
# ---------------------------------------------------------------
def find_vkospi_ticker():
    for market in ["KRX", "KOSPI", "테마"]:
        try:
            for t in stock.get_index_ticker_list(market=market):
                name = stock.get_index_ticker_name(t)
                if "변동성" in name:
                    return t
        except Exception:
            continue
    return None


def indicator_volatility():
    t = find_vkospi_ticker()
    if t is None:
        return None
    start, end = date_range(120)
    df = stock.get_index_ohlcv_by_date(start, end, t)
    close = df["종가"]
    if len(close) < 50:
        return None
    ma50 = close.tail(50).mean()
    cur = float(close.iloc[-1])
    dev = (cur / ma50 - 1) * 100
    return {
        "score": map_range(dev, 45, -30),
        "detail": f"VKOSPI {cur:.1f} / 50일 평균 대비 {dev:+.0f}%",
        "raw": {"vkospi": cur, "vma50": round(float(ma50), 2)},
    }


# ---------------------------------------------------------------
# 지표 6. 안전자산 수요: 코스피 vs 국고채10년 ETF, 20일 수익률차
# ---------------------------------------------------------------
BOND_ETF = "148070"  # KOSEF 국고채10년


def indicator_safehaven():
    start, end = date_range(60)
    kospi = stock.get_index_ohlcv_by_date(start, end, "1001")["종가"]
    bond = stock.get_market_ohlcv_by_date(start, end, BOND_ETF)["종가"]
    if len(kospi) < 21 or len(bond) < 21:
        return None
    k_ret = (kospi.iloc[-1] / kospi.iloc[-21] - 1) * 100
    b_ret = (bond.iloc[-1] / bond.iloc[-21] - 1) * 100
    gap = float(k_ret - b_ret)
    return {
        "score": map_range(gap, -6, 6),
        "detail": f"주식-채권 20일 수익률차 {gap:+.1f}%p",
        "raw": {"gap": round(gap, 2)},
    }


# ---------------------------------------------------------------
# 지표 8. 외국인 수급: 20일 누적 순매수 (코스피)
# ---------------------------------------------------------------
def indicator_foreign():
    start, end = date_range(45)
    df = stock.get_market_trading_value_by_date(start, end, "KOSPI")
    col = "외국인합계" if "외국인합계" in df.columns else "외국인"
    flow_won = float(df[col].tail(20).sum())  # 원 단위
    flow_jo = flow_won / 1e12
    return {
        "score": map_range(flow_jo, -4, 4),
        "detail": f"외국인 20일 누적 {flow_jo:+.2f}조원",
        "raw": {"flow": round(flow_jo, 2)},
    }


# ---------------------------------------------------------------
# 지표 7. 크레딧 스프레드: ECOS API (키 있을 때만)
# ECOS 통계코드 721Y001(시장금리·일별)의 국고채3년/회사채BBB-3년 항목코드는
# ecos.bok.or.kr 에서 확인 후 아래 상수를 채우세요.
# ---------------------------------------------------------------
ECOS_GOV3 = os.environ.get("ECOS_ITEM_GOV3", "")    # 예: 국고채 3년 항목코드
ECOS_BBB3 = os.environ.get("ECOS_ITEM_BBB3", "")    # 예: 회사채 BBB- 3년 항목코드


def indicator_credit():
    key = os.environ.get("ECOS_KEY", "")
    if not (key and ECOS_GOV3 and ECOS_BBB3):
        return None
    import urllib.request

    def latest_rate(item):
        s = (TODAY - timedelta(days=14)).strftime("%Y%m%d")
        e = TODAY.strftime("%Y%m%d")
        url = (f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/50/"
               f"721Y001/D/{s}/{e}/{item}")
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.loads(r.read().decode())
        rows = data["StatisticSearch"]["row"]
        return float(rows[-1]["DATA_VALUE"])

    spread = latest_rate(ECOS_BBB3) - latest_rate(ECOS_GOV3)
    return {
        "score": map_range(spread, 8.0, 6.0),
        "detail": f"BBB- 스프레드 {spread:.2f}%p",
        "raw": {"spread": round(spread, 2)},
    }


# ---------------------------------------------------------------
# 수동 지표: overrides.json (풋/콜, 신고/신저 등 직접 입력값)
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
            "detail": f"풋/콜 {float(v):.2f} (수동)", "raw": {"pcr": v}}


def indicator_strength(ov):
    h, l = ov.get("highs"), ov.get("lows")
    if h is None or l is None or (h + l) == 0:
        return None
    ratio = h / (h + l) * 100
    return {"score": ratio,
            "detail": f"신고가 비중 {ratio:.0f}% (수동)", "raw": {"highs": h, "lows": l}}


# ---------------------------------------------------------------
# 메인
# ---------------------------------------------------------------
def main():
    ov = load_overrides()
    indicators = {
        "momentum":  {"name": "시장 모멘텀 (코스피 vs 125일선)", "res": safe(indicator_momentum)},
        "strength":  {"name": "주가 강도 (52주 신고/신저)",     "res": indicator_strength(ov)},
        "breadth":   {"name": "시장 폭 (ADR 20일)",             "res": safe(indicator_breadth)},
        "putcall":   {"name": "풋/콜 비율 (K200 옵션)",          "res": indicator_putcall(ov)},
        "vol":       {"name": "변동성 (VKOSPI vs 50일 평균)",    "res": safe(indicator_volatility)},
        "safehaven": {"name": "안전자산 수요 (주식 vs 국채)",     "res": safe(indicator_safehaven)},
        "credit":    {"name": "크레딧 스프레드 (BBB- - 국고3년)", "res": safe(indicator_credit)},
        "foreign":   {"name": "외국인 수급 (20일 누적)",          "res": safe(indicator_foreign)},
    }

    scores = [v["res"]["score"] for v in indicators.values() if v["res"]]
    composite = round(sum(scores) / len(scores)) if scores else None

    # 기존 데이터 로드 후 히스토리 이어붙이기
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
        history = history[-60:]  # 최근 60일 보관

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

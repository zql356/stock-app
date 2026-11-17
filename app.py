"""
智能选股推荐APP · 后端API服务（真实A股数据版）
数据源：东方财富网公开API（零第三方依赖）
启动后访问 http://127.0.0.1:5000
"""
import os
import time
import threading
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory

from stock_screener_engine import (
    StockScreener, ScreenResult, Stock,
    STYLE_QUALITY, STYLE_GROWTH, STYLE_DIVIDEND,
    HardFilter, LowReboundChecker, RiskChecker,
    MarketCapTier, TIER_PARAMS, Verdict,
)
from real_data_source import get_cached_market, scan_real_market

app = Flask(__name__, static_folder="static", static_url_path="")

# ============================================================
# 全局状态
# ============================================================

STYLES = {"quality": STYLE_QUALITY, "growth": STYLE_GROWTH, "dividend": STYLE_DIVIDEND}
STYLE_LABELS = {"quality": "质量价值", "growth": "成长", "dividend": "红利"}

USER_CONFIG = {
    "style": "quality",
    "max_price": float(os.environ.get("MAX_PRICE", 40.0)),
    "max_consecutive_limit": 2,
    "min_rise_prob": float(os.environ.get("MIN_RISE_PROB", 70.0)),
    "top_n": 10,
    # 扫描样本数：云端免费层资源受限，用环境变量调小（如 SCAN_COUNT=80）
    "scan_count": int(os.environ.get("SCAN_COUNT", 150)),
}

WATCHLIST = []
REVIEW_LOGS = []

# 数据缓存
_market_cache = {"stocks": [], "timestamp": 0, "loading": False, "progress": ""}


# ============================================================
# 工具函数
# ============================================================

def _build_screener(min_prob=None):
    return StockScreener(
        style=STYLES[USER_CONFIG["style"]],
        max_price=USER_CONFIG["max_price"],
        max_consecutive_limit=USER_CONFIG["max_consecutive_limit"],
        min_rise_prob=min_prob if min_prob is not None else USER_CONFIG["min_rise_prob"],
    )


def _detect_driver(s, r):
    drivers = []
    if s.rev_yoy_last2q > 20 and s.adj_profit_yoy_last2q > 20:
        drivers.append({"name": "业绩超预期", "duration": "1-3季度", "fail_signal": "增速下修、毛利率下滑"})
    if s.gross_margin > 35 and s.gross_margin_volatility < 4:
        drivers.append({"name": "产品涨价/供需缺口", "duration": "1-4季度", "fail_signal": "价格滞涨、库存回升"})
    if s.policy_support:
        drivers.append({"name": "政策驱动", "duration": "3-15交易日", "fail_signal": "细则不及预期"})
    if s.main_inflow_ratio > 5 and s.inflow_days_ratio > 0.6:
        drivers.append({"name": "资金驱动", "duration": "3-10交易日", "fail_signal": "成交额萎缩、龙虎榜转卖"})
    if r.low_rebound_signals >= 4 and s.drawdown_from_history_high > 50:
        drivers.append({"name": "周期反转", "duration": "2-6季度", "fail_signal": "价格二次探底"})
    if s.sector_inflow_rank <= 3:
        drivers.append({"name": "板块联动", "duration": "2-7交易日", "fail_signal": "龙头炸板"})
    if s.pe_5y_percentile < 20 and s.dividend_yield > 3:
        drivers.append({"name": "估值修复", "duration": "数周至数月", "fail_signal": "利率回升、成交萎缩"})
    # 真实数据额外驱动
    if s.drawdown_from_high >= 30 and s.ma20_slope_up:
        drivers.append({"name": "超跌反弹", "duration": "短周期", "fail_signal": "跌破前低"})
    if s.main_inflow > 0 and s.inflow_days_ratio >= 0.5:
        drivers.append({"name": "主力回流", "duration": "3-10交易日", "fail_signal": "净流入转负"})
    if not drivers:
        drivers.append({"name": "技术面反弹", "duration": "短周期", "fail_signal": "跌破支撑位"})
    return drivers[0]


def _tier_label(mv):
    if mv < 100: return "小盘"
    elif mv <= 500: return "中盘"
    return "大盘"


def _result_to_dict(r, include_detail=True):
    s = r.stock
    base = {
        "code": s.code, "name": s.name, "price": s.price,
        "verdict": r.verdict.value,
        "rise_probability": round(r.rise_probability, 1),
        "final_rank": round(r.final_rank, 2),
        "bcd_score": round(r.total_bcd_score, 2),
        "capital_score": round(r.capital_total_score, 2),
        "pv_score": round(r.pv_score, 1),
        "pv_pass": r.pv_pass,
        "low_rebound_signals": r.low_rebound_signals,
        "risk_hits": r.risk_hits,
        "capital_veto": r.capital_veto,
        "event_veto": r.event_veto,
        "hard_pass": r.hard_pass,
        "hard_fail_reasons": r.hard_fail_reasons,
        "drawdown_from_high": s.drawdown_from_high,
        "drawdown_from_history_high": s.drawdown_from_history_high,
        "pe_5y_percentile": s.pe_5y_percentile,
        "pe_ttm": s.pe_ttm,
        "dividend_yield": s.dividend_yield,
        "vol_ratio": s.vol_ratio,
        "turnover_today": s.turnover_today,
        "main_inflow": s.main_inflow,
        "main_inflow_ratio": s.main_inflow_ratio,
        "cum_inflow_5d": s.cum_inflow_5d,
        "inflow_days_ratio": s.inflow_days_ratio,
        "total_mv": s.total_mv,
        "roe_3y_avg": s.roe_3y_avg,
        "gross_margin": s.gross_margin,
        "net_margin": s.net_margin,
        "debt_ratio": s.debt_ratio,
        "pb": s.pb,
        "rev_cagr_3y": s.rev_cagr_3y,
        "peg": s.peg,
        "consecutive_limit_up": s.consecutive_limit_up,
        "ma20": s.ma20, "ma60": s.ma60,
        "ma20_slope_up": s.ma20_slope_up,
        "industry": getattr(s, "_industry", ""),
    }
    if include_detail and r.low_rebound_signals > 0:
        base["low_rebound_hits"] = LowReboundChecker.check(s)
    if include_detail:
        base["driver"] = _detect_driver(s, r)
        base["quality_items"] = [{"id": i.item_id, "name": i.name, "weight": i.weight, "score": i.score, "reason": i.reason} for i in r.quality_scores]
        base["growth_items"] = [{"id": i.item_id, "name": i.name, "weight": i.weight, "score": i.score, "reason": i.reason} for i in r.growth_scores]
        base["valuation_items"] = [{"id": i.item_id, "name": i.name, "weight": i.weight, "score": i.score, "reason": i.reason} for i in r.valuation_scores]
        base["capital_items"] = [{"id": i.item_id, "name": i.name, "weight": i.weight, "score": i.score} for i in r.capital_scores]
    return base


# ============================================================
# 后台扫描线程
# ============================================================

def _refresh_market_async():
    """后台刷新市场数据"""
    if _market_cache["loading"]:
        return
    _market_cache["loading"] = True
    _market_cache["progress"] = "正在扫描A股市场..."
    try:
        stocks = scan_real_market(
            max_price=USER_CONFIG["max_price"],
            max_count=USER_CONFIG["scan_count"],
        )
        _market_cache["stocks"] = stocks
        _market_cache["timestamp"] = time.time()
        _market_cache["progress"] = f"完成：{len(stocks)}支（{datetime.now().strftime('%H:%M')}更新）"
    except Exception as e:
        _market_cache["progress"] = f"扫描失败: {e}"
    finally:
        _market_cache["loading"] = False


def _get_market_stocks(force_refresh=False):
    """获取市场数据，必要时后台刷新"""
    now = time.time()
    cache_ttl = 3600  # 1小时缓存
    if force_refresh or not _market_cache["stocks"] or (now - _market_cache["timestamp"]) > cache_ttl:
        if not _market_cache["loading"]:
            t = threading.Thread(target=_refresh_market_async, daemon=True)
            t.start()
    return _market_cache["stocks"]


# ============================================================
# API 路由
# ============================================================

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/manifest.json")
def manifest():
    return send_from_directory("static", "manifest.json")


@app.route("/sw.js")
def sw():
    return send_from_directory("static", "sw.js")


@app.route("/health")
def health_check():
    """云平台健康检查端点（轻量，不触发数据扫描）"""
    return "ok", 200, {"Content-Type": "text/plain"}


@app.route("/api/status")
def api_status():
    """数据加载状态"""
    return jsonify({
        "code": 0,
        "data": {
            "loading": _market_cache["loading"],
            "progress": _market_cache["progress"],
            "cached_count": len(_market_cache["stocks"]),
            "last_update": datetime.fromtimestamp(_market_cache["timestamp"]).strftime("%Y-%m-%d %H:%M:%S") if _market_cache["timestamp"] else "未加载",
        }
    })


@app.route("/api/recommend")
def api_recommend():
    """推荐接口：严格模式≥70%，无结果时自动降级到宽松模式≥40%"""
    stocks = _get_market_stocks()
    if not stocks:
        return jsonify({"code": 0, "data": {
            "recommends": [], "total_pool": 0, "qualified": 0,
            "config": USER_CONFIG, "style_label": STYLE_LABELS[USER_CONFIG["style"]],
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": "loading", "message": "正在加载真实A股数据，请稍候...",
        }})

    strict_prob = USER_CONFIG["min_rise_prob"]
    screener_strict = _build_screener(min_prob=strict_prob)
    strict_results = screener_strict.screen_batch(stocks, top_n=USER_CONFIG["top_n"])

    if strict_results:
        mode = "strict"
        results = strict_results
        message = f"严格模式：概率≥{strict_prob}%，推荐{len(results)}支"
    else:
        # 宽松模式：降到40%
        loose_prob = 40.0
        screener_loose = _build_screener(min_prob=loose_prob)
        results = screener_loose.screen_batch(stocks, top_n=USER_CONFIG["top_n"])
        mode = "loose"
        message = f"当前市场无概率≥{strict_prob}%的标的，已降级为宽松模式（≥{loose_prob}%），推荐{len(results)}支。建议等待市场回调。"

    return jsonify({
        "code": 0,
        "data": {
            "recommends": [_result_to_dict(r, True) for r in results],
            "total_pool": len(stocks),
            "qualified": len(results),
            "config": USER_CONFIG,
            "style_label": STYLE_LABELS[USER_CONFIG["style"]],
            "scan_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "mode": mode,
            "message": message,
        }
    })


@app.route("/api/screener", methods=["POST"])
def api_screener():
    code = request.json.get("code", "")
    stocks = _get_market_stocks()
    stock = next((s for s in stocks if s.code == code), None)
    if not stock:
        return jsonify({"code": 1, "msg": f"未找到代码 {code}"})
    screener = _build_screener()
    r = screener.screen_one(stock)
    return jsonify({"code": 0, "data": _result_to_dict(r, True)})


@app.route("/api/pool")
def api_pool():
    keyword = request.args.get("keyword", "")
    stocks = _get_market_stocks()
    items = []
    for s in stocks:
        if keyword and keyword not in s.name and keyword not in s.code:
            continue
        items.append({
            "code": s.code, "name": s.name, "price": s.price,
            "total_mv": s.total_mv, "tier": _tier_label(s.total_mv),
            "is_st": s.is_st, "consecutive_limit_up": s.consecutive_limit_up,
            "pe": s.pe_ttm, "roe": s.roe_3y_avg, "turnover": s.turnover_today,
        })
    return jsonify({"code": 0, "data": {"items": items, "total": len(items)}})


@app.route("/api/params", methods=["GET", "POST"])
def api_params():
    global USER_CONFIG
    if request.method == "POST":
        body = request.json or {}
        old_count = USER_CONFIG.get("scan_count")
        for k in ["max_price", "max_consecutive_limit", "min_rise_prob", "top_n", "style", "scan_count"]:
            if k in body:
                USER_CONFIG[k] = body[k]
        # 如果扫描样本数变了，清缓存触发重扫
        if "scan_count" in body and body["scan_count"] != old_count:
            _market_cache["stocks"] = []
            _market_cache["timestamp"] = 0
        return jsonify({"code": 0, "msg": "参数已更新", "data": USER_CONFIG})
    return jsonify({
        "code": 0,
        "data": {
            "config": USER_CONFIG,
            "style_label": STYLE_LABELS[USER_CONFIG["style"]],
            "styles": [{"key": k, "label": v} for k, v in STYLE_LABELS.items()],
        }
    })


@app.route("/api/tier_params")
def api_tier_params():
    mv = float(request.args.get("mv", 100))
    tier = TIER_PARAMS[MarketCapTier.SMALL if mv < 100 else (MarketCapTier.MID if mv <= 500 else MarketCapTier.LARGE)]
    return jsonify({"code": 0, "data": {
        "tier": tier.tier.value, "vol_ratio_low": tier.vol_ratio_low,
        "vol_ratio_high": tier.vol_ratio_high, "turnover_low": tier.turnover_low,
        "turnover_high": tier.turnover_high, "main_inflow_ratio_low": tier.main_inflow_ratio_low,
        "inflow_days_ratio_low": tier.inflow_days_ratio_low, "cum_inflow_5d_low": tier.cum_inflow_5d_low,
        "turnover_overheat": tier.turnover_overheat,
    }})


@app.route("/api/dashboard")
def api_dashboard():
    stocks = _get_market_stocks()
    items = []
    for s in stocks[:50]:
        tier = TIER_PARAMS[MarketCapTier.SMALL if s.total_mv < 100 else (MarketCapTier.MID if s.total_mv <= 500 else MarketCapTier.LARGE)]
        vol_ok = tier.vol_ratio_low <= s.vol_ratio <= tier.vol_ratio_high
        turnover_ok = tier.turnover_low <= s.turnover_today <= tier.turnover_high
        inflow_ok = s.main_inflow_ratio >= tier.main_inflow_ratio_low
        cum_ok = s.cum_inflow_5d >= tier.cum_inflow_5d_low
        days_ok = s.inflow_days_ratio * 100 >= tier.inflow_days_ratio_low
        ma_ok = s.price > s.ma20
        satisfied = sum([vol_ok, turnover_ok, inflow_ok, cum_ok, days_ok, ma_ok])
        signal = "可入场" if satisfied >= 5 else ("观察" if satisfied >= 3 else "回避")
        items.append({
            "code": s.code, "name": s.name, "price": s.price,
            "total_mv": s.total_mv, "tier": tier.tier.value,
            "vol_ratio": s.vol_ratio, "turnover": s.turnover_today,
            "main_inflow": s.main_inflow, "main_inflow_ratio": s.main_inflow_ratio,
            "cum_inflow_5d": s.cum_inflow_5d, "inflow_days_ratio": round(s.inflow_days_ratio * 100, 1),
            "above_ma20": s.price > s.ma20,
            "vol_ok": vol_ok, "turnover_ok": turnover_ok, "inflow_ok": inflow_ok,
            "cum_ok": cum_ok, "days_ok": days_ok, "ma_ok": ma_ok,
            "satisfied": satisfied, "signal": signal,
        })
    items.sort(key=lambda x: x["satisfied"], reverse=True)
    return jsonify({"code": 0, "data": {"items": items}})


@app.route("/api/review", methods=["GET", "POST"])
def api_review():
    global REVIEW_LOGS
    if request.method == "POST":
        body = request.json or {}
        body["id"] = len(REVIEW_LOGS) + 1
        body["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        REVIEW_LOGS.append(body)
        return jsonify({"code": 0, "msg": "复盘记录已保存"})
    return jsonify({"code": 0, "data": {"items": REVIEW_LOGS}})


@app.route("/api/watchlist", methods=["GET", "POST", "DELETE"])
def api_watchlist():
    global WATCHLIST
    if request.method == "POST":
        code = request.json.get("code", "")
        if code and code not in WATCHLIST:
            WATCHLIST.append(code)
        return jsonify({"code": 0, "msg": "已添加自选"})
    if request.method == "DELETE":
        code = request.args.get("code", "")
        if code in WATCHLIST:
            WATCHLIST.remove(code)
        return jsonify({"code": 0, "msg": "已移除自选"})
    stocks = _get_market_stocks()
    items = []
    for code in WATCHLIST:
        s = next((x for x in stocks if x.code == code), None)
        if s:
            items.append({"code": s.code, "name": s.name, "price": s.price,
                          "total_mv": s.total_mv, "drawdown_from_high": s.drawdown_from_high})
    return jsonify({"code": 0, "data": {"items": items}})


@app.route("/api/scan_all")
def api_scan_all():
    stocks = _get_market_stocks()
    if not stocks:
        return jsonify({"code": 0, "data": {"total": 0, "message": "数据加载中"}})
    screener = _build_screener()
    all_results = [screener.screen_one(s) for s in stocks]
    stats = {
        "total": len(all_results),
        "hard_pass": sum(1 for r in all_results if r.hard_pass),
        "hard_fail": sum(1 for r in all_results if r.verdict == Verdict.REJECT_HARD),
        "risk_reject": sum(1 for r in all_results if r.verdict == Verdict.REJECT_RISK),
        "overheat_reject": sum(1 for r in all_results if r.verdict == Verdict.REJECT_OVERHEAT),
        "watch": sum(1 for r in all_results if r.verdict == Verdict.WATCH),
        "core": sum(1 for r in all_results if r.verdict == Verdict.CORE),
        "prob_ge_70": sum(1 for r in all_results if r.rise_probability >= 70),
        "prob_ge_50": sum(1 for r in all_results if r.rise_probability >= 50),
        "prob_ge_40": sum(1 for r in all_results if r.rise_probability >= 40),
    }
    stats["final_recommend"] = stats["core"] + stats["watch"]
    prob_buckets = {"<30": 0, "30-40": 0, "40-50": 0, "50-60": 0, "60-70": 0, "70+": 0}
    for r in all_results:
        p = r.rise_probability
        if p < 30: prob_buckets["<30"] += 1
        elif p < 40: prob_buckets["30-40"] += 1
        elif p < 50: prob_buckets["40-50"] += 1
        elif p < 60: prob_buckets["50-60"] += 1
        elif p < 70: prob_buckets["60-70"] += 1
        else: prob_buckets["70+"] += 1
    stats["prob_buckets"] = prob_buckets
    return jsonify({"code": 0, "data": stats})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    """手动刷新市场数据"""
    _market_cache["stocks"] = []
    _market_cache["timestamp"] = 0
    _get_market_stocks(force_refresh=True)
    return jsonify({"code": 0, "msg": "已触发数据刷新，请稍候"})


# ============================================================
# 启动
# ============================================================

# ============================================================
# 启动
# ============================================================

# 确保静态目录存在（Gunicorn/Docker启动时也可用）
os.makedirs(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), exist_ok=True)

if __name__ == "__main__":
    print("=" * 60)
    print("  智能选股推荐APP · 真实A股数据版 启动中")
    print(f"  数据源: 东方财富网公开API")
    print(f"  扫描样本: {USER_CONFIG['scan_count']}支  风格: {STYLE_LABELS[USER_CONFIG['style']]}")
    print(f"  推荐门槛: 价格≤{USER_CONFIG['max_price']}元  概率≥{USER_CONFIG['min_rise_prob']}%")
    print("  访问地址: http://127.0.0.1:5000")
    print("=" * 60)
    # 启动时后台开始扫描
    _get_market_stocks(force_refresh=True)
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)

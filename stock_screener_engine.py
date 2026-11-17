"""
智能选股推荐引擎 — 核心筛选流水线
基于「股票筛选清单」14-Sheet 四层框架编码：底线排除 → 质地 → 成长 → 估值 → 量价 → 资金 → 低位反弹

运行：python stock_screener_engine.py
依赖：pandas, numpy（数据接入层可替换为真实行情/财务数据源）
"""

from __future__ import annotations
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ============================================================
# 数据结构定义
# ============================================================

class MarketCapTier(Enum):
    """市值档位（对应参数区 Sheet13）"""
    SMALL = "小盘"   # <100亿
    MID = "中盘"     # 100-500亿
    LARGE = "大盘"   # >500亿


class Verdict(Enum):
    """单只股票的最终判定"""
    PASS = "通过"
    REJECT_HARD = "硬条件淘汰"
    REJECT_RISK = "风险否决"
    REJECT_OVERHEAT = "过热回避"
    WATCH = "观察池"
    CORE = "核心池"


@dataclass
class TierParams:
    """参数区（Sheet13）—— 按市值档位联动的阈值"""
    tier: MarketCapTier
    vol_ratio_low: float          # 量比下限
    vol_ratio_high: float         # 量比上限
    turnover_low: float           # 换手率下限 %
    turnover_high: float          # 换手率上限 %
    vol_multiple: float           # 突破日成交额倍数（≥5日均量）
    main_inflow_ratio_low: float  # 主力净流入占成交额下限 %
    inflow_days_ratio_low: float  # 净流入天数占比下限 %
    cum_inflow_5d_low: float      # 5日累计净流入下限 亿元
    turnover_overheat: float      # 换手过热警戒 %


# 三档参数（取自 Sheet13 参数区）
TIER_PARAMS = {
    MarketCapTier.SMALL: TierParams(MarketCapTier.SMALL, 1.5, 3.0, 5, 20, 1.5, 5, 60, 0.5, 30),
    MarketCapTier.MID:   TierParams(MarketCapTier.MID,   1.2, 3.0, 3, 15, 1.5, 5, 60, 1.0, 25),
    MarketCapTier.LARGE: TierParams(MarketCapTier.LARGE, 1.0, 2.5, 1.5, 8, 1.5, 3, 60, 2.0, 15),
}


@dataclass
class Stock:
    """一只股票的全部输入字段（行情 + 财务 + 资金 + 技术面）"""
    code: str                          # 6位代码
    name: str                          # 简称
    price: float                       # 最新收盘价 元
    is_st: bool = False                # ST/*ST
    list_years: float = 0              # 上市年限
    total_mv: float = 0.0              # 总市值 亿元
    avg_turnover_3m: float = 0.0       # 近3月日均成交额 亿元
    audit_qualified_3y: bool = True    # 近3年标准无保留审计意见
    no_investigation: bool = True      # 无立案调查/近2年无监管处罚
    no_major_lawsuit: bool = True      # 无占净资产10%以上诉讼
    goodwill_to_nav: float = 0.0       # 商誉/净资产 %
    debt_ratio: float = 0.0            # 资产负债率 %
    ocf_3y_sum: float = 0.0            # 近3年经营现金流累计 亿元
    net_profit_3y_sum: float = 0.0     # 近3年净利润累计 亿元
    # B段 质地
    roe_3y_avg: float = 0.0
    roe_min: float = 0.0
    equity_multiplier: float = 0.0
    gross_margin: float = 0.0
    gross_margin_volatility: float = 0.0  # 近3年波动 pct
    gross_margin_vs_industry: float = 0.0 # 相对行业中位数 pct
    net_margin: float = 0.0
    roic: float = 0.0
    rev_cagr_3y: float = 0.0
    adj_profit_cagr_3y: float = 0.0
    ar_to_rev: float = 0.0
    inventory_turnover_days: float = 0.0
    industry_inv_turnover_days: float = 0.0
    cash_to_rev_ratio: float = 0.0
    ocf_to_np: float = 0.0
    pledge_ratio: float = 0.0
    no_major_reduction: bool = True
    # C段 成长
    rev_yoy_last2q: float = 0.0       # 最近2季营收同比 %
    rev_yoy_prev: float = 0.0         # 前两季营收同比 %
    adj_profit_yoy_last2q: float = 0.0
    adj_profit_yoy_prev: float = 0.0
    industry_up: bool = False         # 行业景气上行
    policy_support: bool = False
    rd_ratio: float = 0.0
    is_tech: bool = False
    # D段 估值
    pe_ttm: float = 0.0
    pe_industry_median: float = 0.0
    pe_5y_percentile: float = 0.0     # 自身5年分位 %
    peg: float = 0.0
    pb: float = 0.0
    dividend_yield: float = 0.0
    fcf_to_mv: float = 0.0
    high_52w: float = 0.0
    drawdown_from_high: float = 0.0   # 距52周高点回撤 %
    # 量价
    ma20: float = 0.0
    ma60: float = 0.0
    ma20_slope_up: bool = False
    rank_20d_in_industry: float = 0.0  # 近20日涨幅行业排名百分位
    breakthrough_vol_multiple: float = 0.0  # 突破日量/5日均量
    upper_shadow_to_body: float = 0.0
    pullback_vol_ratio: float = 0.0    # 回调量/突破量
    ma_converge: bool = False          # 均线粘合
    turnover_today: float = 0.0
    has_gap_up_unfilled: bool = False
    consecutive_limit_up: int = 0      # 连续涨停天数
    # 资金
    vol_ratio: float = 0.0
    main_inflow: float = 0.0           # 亿元
    main_inflow_ratio: float = 0.0     # 占成交额 %
    cum_inflow_5d: float = 0.0
    inflow_days_ratio: float = 0.0     # 净流入天数占比 %
    big_order_healthy: bool = False
    dragon_tiger_inst: int = 0         # 龙虎榜机构家数
    north_holding_up_days: int = 0
    margin_change_pct: float = 0.0
    sector_inflow_rank: int = 0
    # 低位反弹
    drawdown_from_history_high: float = 0.0  # 距历史高点回撤 %
    price_percentile_3y: float = 0.0         # 近3年股价分位 %
    pe_5y_percentile_low: float = 0.0
    shareholder_count_down_2q: bool = False
    inst_holding_low: bool = False
    ground_volume_days: int = 0              # 地量天数
    bottom_surge_vol_multiple: float = 0.0   # 底部放量倍数
    low_elevated: bool = False               # 低点抬高
    macd_divergence: bool = False
    bad_news_exhausted: bool = False
    # E段 风控
    unlock_ratio_3m: float = 0.0     # 未来3月解禁/流通市值 %
    has_earning_warning: bool = False
    has_inquiry_letter: bool = False
    has_new_lawsuit: bool = False


@dataclass
class ScoreItem:
    """单项打分结果"""
    item_id: str
    name: str
    weight: int
    score: float          # 0-10
    weighted: float       # score * weight / 10
    reason: str = ""


@dataclass
class ScreenResult:
    """单只股票的完整筛选结果"""
    stock: Stock
    hard_pass: bool = False
    hard_fail_reasons: list[str] = field(default_factory=list)
    quality_scores: list[ScoreItem] = field(default_factory=list)
    growth_scores: list[ScoreItem] = field(default_factory=list)
    valuation_scores: list[ScoreItem] = field(default_factory=list)
    capital_scores: list[ScoreItem] = field(default_factory=list)
    total_bcd_score: float = 0.0       # B+C+D 加权总分 0-10
    capital_total_score: float = 0.0   # 资金打分 0-10
    pv_pass: bool = False              # 量价必要条件
    pv_score: float = 0.0
    low_rebound_signals: int = 0       # 低位反弹触发信号数
    risk_hits: int = 0                 # 风险核查命中数
    capital_veto: bool = False         # 资金否决
    event_veto: bool = False           # E2事件否决
    verdict: Verdict = Verdict.REJECT_HARD
    rise_probability: float = 0.0      # 上涨概率 %
    final_rank: float = 0.0            # 综合排序分


# ============================================================
# 风格参数（Sheet4）
# ============================================================

@dataclass
class StyleParams:
    style: str
    roe_min: float
    rev_cagr_min: float
    adj_profit_cagr_min: float
    gross_margin_min: float
    debt_ratio_max: float
    pe_requirement: str        # "percentile" / "peg" / "none"
    dividend_yield_min: float
    fcf_required: bool
    rd_required: bool

STYLE_QUALITY = StyleParams("质量价值", 15, 8, 10, 30, 50, "percentile", 2, True, False)
STYLE_GROWTH  = StyleParams("成长",     12, 20, 20, 25, 60, "peg",       0, False, True)
STYLE_DIVIDEND= StyleParams("红利",     10, 0,  0,  20, 60, "none",      3, True, False)


# ============================================================
# A段 · 底线排除（一票否决）
# ============================================================

class HardFilter:
    """A段8项硬条件，任一不满足直接淘汰"""

    @staticmethod
    def check(s: Stock) -> tuple[bool, list[str]]:
        fails = []
        if s.is_st:
            fails.append("A1 ST/退市风险警示")
        if s.list_years < 3:
            fails.append("A2 上市不满3年")
        if s.total_mv < 50:
            fails.append("A3 总市值<50亿")
        if s.avg_turnover_3m < 0.5:
            fails.append("A3 近3月日均成交额<5000万")
        if not s.audit_qualified_3y:
            fails.append("A4 审计意见非标准无保留")
        if not s.no_investigation:
            fails.append("A5 立案调查/监管处罚")
        if not s.no_major_lawsuit:
            fails.append("A5 重大诉讼")
        if s.goodwill_to_nav > 30:
            fails.append("A6 商誉/净资产>30%")
        if s.debt_ratio > 60:
            fails.append("A7 资产负债率>60%")
        if s.ocf_3y_sum <= 0 or (s.net_profit_3y_sum > 0 and s.ocf_3y_sum < 0.7 * s.net_profit_3y_sum):
            fails.append("A8 经营现金流含金量不足")
        return (len(fails) == 0, fails)


# ============================================================
# B段 · 质地打分（40分）
# ============================================================

class QualityScorer:
    WEIGHTS = {"B1": 8, "B2": 6, "B3": 6, "B4": 8, "B5": 5, "B6": 7}

    @staticmethod
    def score(s: Stock) -> list[ScoreItem]:
        items = []
        # B1 ROE
        b1 = 0
        if s.roe_3y_avg >= 15 and s.roe_min >= 12:
            b1 = 10
        elif s.roe_3y_avg >= 12:
            b1 = 7
        elif s.roe_3y_avg >= 8:
            b1 = 4
        if s.equity_multiplier >= 2.5:
            b1 = max(0, b1 - 3)
        items.append(ScoreItem("B1", "ROE水平", 8, b1, b1 * 8 / 10,
                               f"3年均值{s.roe_3y_avg:.1f}%"))

        # B2 毛利率
        b2 = 0
        if s.gross_margin >= 30 and s.gross_margin_volatility < 5:
            b2 = 10
        elif s.gross_margin >= 30 or s.gross_margin_volatility < 5:
            b2 = 6
        if s.gross_margin < s.gross_margin_vs_industry:
            b2 = max(0, b2 - 2)
        if s.gross_margin < 20:
            b2 = 0
        items.append(ScoreItem("B2", "毛利率与定价权", 6, b2, b2 * 6 / 10,
                               f"毛利率{s.gross_margin:.1f}%"))

        # B3 盈利质量
        b3 = 0
        np_ok = s.net_margin >= 8
        roic_ok = s.roic >= 10
        if np_ok and roic_ok:
            b3 = 10
        elif np_ok or roic_ok:
            b3 = 5
        items.append(ScoreItem("B3", "盈利质量", 6, b3, b3 * 6 / 10,
                               f"净利率{s.net_margin:.1f}% ROIC{s.roic:.1f}%"))

        # B4 中期成长
        b4 = 0
        rev_ok = s.rev_cagr_3y >= 8
        profit_ok = s.adj_profit_cagr_3y >= 10
        if rev_ok and profit_ok:
            b4 = 10
        elif rev_ok or profit_ok:
            b4 = 5
        if s.rev_cagr_3y < 0 or s.adj_profit_cagr_3y < 0:
            b4 = 0
        items.append(ScoreItem("B4", "中期成长", 8, b4, b4 * 8 / 10,
                               f"营收CAGR{s.rev_cagr_3y:.1f}%"))

        # B5 营运效率
        checks = [
            s.ar_to_rev < 30,
            s.inventory_turnover_days <= s.industry_inv_turnover_days,
            s.cash_to_rev_ratio >= 1,
        ]
        b5 = [0, 4, 7, 10][sum(checks)]
        items.append(ScoreItem("B5", "营运效率", 5, b5, b5 * 5 / 10,
                               f"应收/营收{s.ar_to_rev:.1f}%"))

        # B6 现金流与治理
        b6 = 0
        if s.ocf_to_np >= 1 and s.pledge_ratio < 10:
            b6 = 10
        elif s.ocf_to_np >= 0.7:
            b6 = 7
        if not s.no_major_reduction:
            b6 = max(0, b6 - 3)
        items.append(ScoreItem("B6", "现金流与治理", 7, b6, b6 * 7 / 10,
                               f"OCF/净利{s.ocf_to_np:.2f}"))
        return items


# ============================================================
# C段 · 成长与景气打分（30分）
# ============================================================

class GrowthScorer:
    WEIGHTS = {"C1": 8, "C2": 8, "C3": 8, "C4": 6}

    @staticmethod
    def score(s: Stock) -> list[ScoreItem]:
        items = []
        # C1 收入加速
        c1 = 0
        if s.rev_yoy_last2q > 25:
            c1 = 10
        elif s.rev_yoy_last2q > 15:
            c1 = 7
        elif s.rev_yoy_last2q > 0:
            c1 = 4
        if s.rev_yoy_last2q <= s.rev_yoy_prev:
            c1 = max(0, c1 - 2)
        items.append(ScoreItem("C1", "收入加速", 8, c1, c1 * 8 / 10,
                               f"近2季营收同比{s.rev_yoy_last2q:.1f}%"))

        # C2 利润加速
        c2 = 0
        if s.adj_profit_yoy_last2q > 25:
            c2 = 10
        elif s.adj_profit_yoy_last2q > 15:
            c2 = 7
        elif s.adj_profit_yoy_last2q > 0:
            c2 = 4
        if s.adj_profit_yoy_last2q <= s.adj_profit_yoy_prev:
            c2 = max(0, c2 - 2)
        items.append(ScoreItem("C2", "利润加速", 8, c2, c2 * 8 / 10,
                               f"近2季扣非同比{s.adj_profit_yoy_last2q:.1f}%"))

        # C3 行业景气
        c3 = 0
        if s.industry_up and s.policy_support:
            c3 = 10
        elif s.industry_up or s.policy_support:
            c3 = 7
        else:
            c3 = 5
        items.append(ScoreItem("C3", "行业景气", 8, c3, c3 * 8 / 10,
                               "上行+政策" if c3 == 10 else ("单一支撑" if c3 == 7 else "平稳")))

        # C4 研发投入
        rd_std = 10 if (s.is_tech and s.name and any(k in s.name for k in ["软件", "药"])) else (5 if s.is_tech else 0)
        c4 = 0
        if s.rd_ratio >= rd_std + 2:
            c4 = 10
        elif s.rd_ratio >= rd_std:
            c4 = 7
        elif s.rd_ratio >= rd_std - 2:
            c4 = 4
        items.append(ScoreItem("C4", "研发投入", 6, c4, c4 * 6 / 10,
                               f"研发费用率{s.rd_ratio:.1f}%"))
        return items


# ============================================================
# D段 · 估值与安全边际打分（30分）
# ============================================================

class ValuationScorer:
    WEIGHTS = {"D1": 12, "D2": 10, "D3": 8}

    @staticmethod
    def score(s: Stock) -> list[ScoreItem]:
        items = []
        # D1 估值分位
        d1 = 0
        below_industry = s.pe_ttm < s.pe_industry_median
        low_percentile = s.pe_5y_percentile < 30
        if below_industry and s.pe_5y_percentile < 20:
            d1 = 10
        elif below_industry or low_percentile:
            d1 = 6
        items.append(ScoreItem("D1", "估值分位", 12, d1, d1 * 12 / 10,
                               f"PE分位{s.pe_5y_percentile:.0f}%"))

        # D2 风格适配
        d2 = 0
        if s.peg < 0.8 or s.pe_5y_percentile < 10 or s.dividend_yield > 4:
            d2 = 10
        elif s.peg < 1 or s.pe_5y_percentile < 30 or s.dividend_yield >= 3:
            d2 = 7
        items.append(ScoreItem("D2", "风格适配估值", 10, d2, d2 * 10 / 10,
                               f"PEG{s.peg:.2f}"))

        # D3 现金回报与位置
        d3 = 0
        fcf_ok = s.fcf_to_mv >= 4
        drawdown_ok = 20 <= s.drawdown_from_high <= 40
        if s.fcf_to_mv >= 6 and drawdown_ok:
            d3 = 10
        elif fcf_ok or drawdown_ok:
            d3 = 7
        items.append(ScoreItem("D3", "现金回报与位置", 8, d3, d3 * 8 / 10,
                               f"FCF/市值{s.fcf_to_mv:.1f}% 回撤{s.drawdown_from_high:.0f}%"))
        return items


# ============================================================
# 量价筛选（Sheet7）
# ============================================================

class PriceVolumeFilter:
    """技术面：必要条件 + 打分项"""

    @staticmethod
    def check_necessary(s: Stock) -> bool:
        """必要条件：收盘价>MA20>MA60 且 MA20斜率向上"""
        return s.price > s.ma20 > s.ma60 and s.ma20_slope_up

    @staticmethod
    def score(s: Stock) -> float:
        total = 0
        # 相对强度（8分）
        if s.rank_20d_in_industry >= 80:
            total += 8
        elif s.rank_20d_in_industry >= 60:
            total += 5
        # 突破形态（8分）
        if s.breakthrough_vol_multiple >= 1.5 and s.upper_shadow_to_body < 2:
            total += 8
        elif s.breakthrough_vol_multiple >= 1.5:
            total += 5
        # 量价配合（6分）
        if s.pullback_vol_ratio < 0.5:
            total += 6
        elif s.pullback_vol_ratio < 0.7:
            total += 3
        # 均线粘合（5分）
        if s.ma_converge and s.ma20_slope_up:
            total += 5
        # 换手承接（4分）
        if 3 <= s.turnover_today <= 15:
            total += 4
        elif 1 <= s.turnover_today <= 20:
            total += 2
        # 缺口加分（3分）
        if s.has_gap_up_unfilled:
            total += 3
        return min(total, 34)


# ============================================================
# 资金打分（Sheet12，14项权重100 + 3项否决）
# ============================================================

class CapitalScorer:
    @staticmethod
    def score(s: Stock, tier: TierParams) -> tuple[list[ScoreItem], bool]:
        """返回(14项打分, 是否否决)"""
        items = []
        veto = False

        # 否决1：过热出货（量比>6且换手过热）
        if s.vol_ratio > 6 and s.turnover_today > tier.turnover_overheat:
            veto = True
        # 否决2：高位放量滞涨（距60日高点<5%且换手>25%且收阴）—— 简化用换手+主力流出
        high_proximity = abs(s.price - s.high_52w) / s.high_52w * 100 if s.high_52w > 0 else 100
        if high_proximity < 5 and s.turnover_today > 25 and s.main_inflow < 0:
            veto = True
        # 否决3：连续3个交易日主力资金净流出（简化：6日里净流出占≥70%）
        if s.inflow_days_ratio < 0.3:
            veto = True

        # 1 主力净流入
        v = 10 if s.main_inflow_ratio >= 10 else (7 if s.main_inflow_ratio >= 5 else (4 if s.main_inflow_ratio >= 0 else 0))
        items.append(ScoreItem("K1", "主力净流入", 10, v, v * 10 / 10))
        # 2 5日累计
        v = 10 if (s.cum_inflow_5d > 0 and s.main_inflow > 0) else (6 if s.cum_inflow_5d > 0 else 0)
        items.append(ScoreItem("K2", "5日累计净流入", 10, v, v * 10 / 10))
        # 3 大单结构
        v = 10 if s.big_order_healthy else 0
        items.append(ScoreItem("K3", "大单结构", 6, v, v * 6 / 10))
        # 4 龙虎榜
        v = 10 if s.dragon_tiger_inst >= 2 else (6 if s.dragon_tiger_inst >= 1 else 0)
        items.append(ScoreItem("K4", "龙虎榜机构", 5, v, v * 5 / 10))
        # 5 北向
        v = 10 if s.north_holding_up_days >= 3 else (5 if s.north_holding_up_days >= 1 else 0)
        items.append(ScoreItem("K5", "北向持股", 5, v, v * 5 / 10))
        # 6 两融
        v = 10 if s.margin_change_pct >= 10 else (7 if s.margin_change_pct >= 5 else 0)
        items.append(ScoreItem("K6", "两融余额", 4, v, v * 4 / 10))
        # 7 量比
        if tier.vol_ratio_low <= s.vol_ratio <= 2.5:
            v = 10
        elif s.vol_ratio <= 3:
            v = 8
        elif 0.8 <= s.vol_ratio < tier.vol_ratio_low:
            v = 5
        else:
            v = 0
        items.append(ScoreItem("K7", "量比", 10, v, v * 10 / 10))
        # 8 换手率
        if 5 <= s.turnover_today <= 15:
            v = 10
        elif 3 <= s.turnover_today < 5:
            v = 7
        elif 15 < s.turnover_today <= 25:
            v = 4
        else:
            v = 0
        items.append(ScoreItem("K8", "换手率", 8, v, v * 8 / 10))
        # 9 突破日成交额
        v = 10 if s.breakthrough_vol_multiple >= 2 else (7 if s.breakthrough_vol_multiple >= 1.5 else (4 if s.breakthrough_vol_multiple >= 1 else 0))
        items.append(ScoreItem("K9", "突破日量能", 8, v, v * 8 / 10))
        # 10 量价配合
        v = 10 if s.pullback_vol_ratio < 0.5 else (6 if s.pullback_vol_ratio < 0.7 else 0)
        items.append(ScoreItem("K10", "量价配合", 9, v, v * 9 / 10))
        # 11 持续性
        if s.inflow_days_ratio >= 0.75:
            v = 10
        elif s.inflow_days_ratio >= 0.6:
            v = 7
        elif s.inflow_days_ratio >= 0.4:
            v = 4
        else:
            v = 0
        items.append(ScoreItem("K11", "资金持续性", 8, v, v * 8 / 10))
        # 12 温和放大（简化）
        v = 10 if 1.2 <= s.vol_ratio <= 2 else 6
        items.append(ScoreItem("K12", "量能温和放大", 6, v, v * 6 / 10))
        # 13 换手倍数
        v = 10 if 1.2 <= s.turnover_today / max(0.1, s.turnover_today) <= 2 else 0
        items.append(ScoreItem("K13", "换手倍数", 6, v, v * 6 / 10))
        # 14 板块资金
        v = 10 if s.sector_inflow_rank <= 3 else (5 if s.sector_inflow_rank <= 10 else 0)
        items.append(ScoreItem("K14", "板块资金占比", 5, v, v * 5 / 10))
        return items, veto


# ============================================================
# 低位反弹检查（Sheet9）
# ============================================================

class LowReboundChecker:
    SIGNALS = [
        "回撤深度达标", "股价分位<10%", "估值分位<10%/破净",
        "筹码集中", "地量见底", "底部放量长阳",
        "低点抬高", "指标背离", "利空出尽", "资金确认",
    ]

    @staticmethod
    def check(s: Stock) -> list[str]:
        hits = []
        if s.drawdown_from_history_high >= 50:
            hits.append("回撤深度达标")
        if s.price_percentile_3y < 10:
            hits.append("股价分位<10%")
        if s.pe_5y_percentile_low < 10 or s.pb < 1:
            hits.append("估值分位<10%/破净")
        if s.shareholder_count_down_2q and s.inst_holding_low:
            hits.append("筹码集中")
        if s.ground_volume_days >= 5:
            hits.append("地量见底")
        if s.bottom_surge_vol_multiple >= 2 and s.price > s.ma20:
            hits.append("底部放量长阳")
        if s.low_elevated:
            hits.append("低点抬高")
        if s.macd_divergence:
            hits.append("指标背离")
        if s.bad_news_exhausted:
            hits.append("利空出尽")
        # 资金确认（增持/回购/股息率>4%）简化用 dividend_yield
        if s.dividend_yield > 4:
            hits.append("资金确认")
        return hits


# ============================================================
# 风险核查（Sheet5）
# ============================================================

class RiskChecker:
    @staticmethod
    def check(s: Stock) -> int:
        hits = 0
        if s.ar_to_rev > 50:
            hits += 1
        if s.ocf_to_np < 0.5 and s.ar_to_rev > 30:
            hits += 1
        if s.pledge_ratio > 40:
            hits += 1
        if s.goodwill_to_nav > 30:
            hits += 1
        return hits


# ============================================================
# 主筛选引擎
# ============================================================

class StockScreener:
    """编排全流程：硬条件 → 打分 → 量价 → 资金 → 低位反弹 → 风险 → 概率"""

    def __init__(self, style: StyleParams = STYLE_QUALITY,
                 max_price: float = 40.0,
                 max_consecutive_limit: int = 2,
                 min_rise_prob: float = 70.0):
        self.style = style
        self.max_price = max_price
        self.max_consecutive_limit = max_consecutive_limit
        self.min_rise_prob = min_rise_prob

    def _tier_of(self, mv: float) -> TierParams:
        if mv < 100:
            return TIER_PARAMS[MarketCapTier.SMALL]
        elif mv <= 500:
            return TIER_PARAMS[MarketCapTier.MID]
        return TIER_PARAMS[MarketCapTier.LARGE]

    def screen_one(self, s: Stock) -> ScreenResult:
        r = ScreenResult(stock=s)

        # 用户定制条件：股价≤40元
        if s.price > self.max_price:
            r.hard_fail_reasons.append(f"股价{s.price:.2f}元超过{self.max_price}元上限")
            r.verdict = Verdict.REJECT_HARD
            return r

        # 用户定制条件：非连续涨3天
        if s.consecutive_limit_up >= 3:
            r.hard_fail_reasons.append(f"连续涨停{s.consecutive_limit_up}天，排除")
            r.verdict = Verdict.REJECT_HARD
            return r

        # A段 硬条件
        ok, fails = HardFilter.check(s)
        r.hard_pass = ok
        r.hard_fail_reasons = fails
        if not ok:
            r.verdict = Verdict.REJECT_HARD
            return r

        # B/C/D 打分
        r.quality_scores = QualityScorer.score(s)
        r.growth_scores = GrowthScorer.score(s)
        r.valuation_scores = ValuationScorer.score(s)
        total_weight = sum(i.weight for i in r.quality_scores + r.growth_scores + r.valuation_scores)
        total_weighted = sum(i.weighted for i in r.quality_scores + r.growth_scores + r.valuation_scores)
        r.total_bcd_score = total_weighted / total_weight * 10

        # E2 事件否决
        if s.has_earning_warning or s.has_inquiry_letter or s.has_new_lawsuit:
            r.event_veto = True
            r.verdict = Verdict.REJECT_RISK
            return r

        # E1 解禁扣分
        if s.unlock_ratio_3m >= 5:
            r.total_bcd_score = max(0, r.total_bcd_score - 0.5)

        # 量价必要条件
        r.pv_pass = PriceVolumeFilter.check_necessary(s)
        r.pv_score = PriceVolumeFilter.score(s)

        # 资金打分
        tier = self._tier_of(s.total_mv)
        cap_items, cap_veto = CapitalScorer.score(s, tier)
        r.capital_scores = cap_items
        r.capital_veto = cap_veto
        cap_weight = sum(i.weight for i in cap_items)
        r.capital_total_score = sum(i.weighted for i in cap_items) / cap_weight * 10

        if cap_veto:
            r.verdict = Verdict.REJECT_OVERHEAT
            return r

        # 低位反弹
        low_hits = LowReboundChecker.check(s)
        r.low_rebound_signals = len(low_hits)

        # 风险核查
        r.risk_hits = RiskChecker.check(s)
        if r.risk_hits >= 2:
            r.verdict = Verdict.REJECT_RISK
            return r

        # 综合判定 + 上涨概率
        r.rise_probability = self._calc_rise_prob(r)
        r.final_rank = self._calc_rank(r)

        if r.total_bcd_score >= 8 and r.capital_total_score >= 8 and r.pv_pass and r.low_rebound_signals >= 3:
            r.verdict = Verdict.CORE
        elif r.total_bcd_score >= 6.5:
            r.verdict = Verdict.WATCH
        else:
            r.verdict = Verdict.REJECT_HARD
        return r

    def _calc_rise_prob(self, r: ScreenResult) -> float:
        """上涨概率 = f(基本面分, 资金分, 量价分, 低位信号, 风险)
        经验权重合成，归一到 0-100%"""
        w = (
            r.total_bcd_score * 0.30 +
            r.capital_total_score * 0.25 +
            r.pv_score / 34 * 10 * 0.20 +
            min(r.low_rebound_signals, 6) / 6 * 10 * 0.15 +
            (10 - r.risk_hits * 3) * 0.10
        )
        prob = w / 10 * 100
        if not r.pv_pass:
            prob -= 15
        if r.low_rebound_signals < 3:
            prob -= 10
        return max(0, min(95, prob))

    def _calc_rank(self, r: ScreenResult) -> float:
        """综合排序分：上涨概率为主，叠加安全边际"""
        safety = (40 - r.stock.price) / 40 * 5  # 价格越低加分
        return r.rise_probability + safety

    def screen_batch(self, stocks: list[Stock], top_n: int = 10) -> list[ScreenResult]:
        """批量筛选并返回 Top-N 推荐"""
        results = [self.screen_one(s) for s in stocks]
        qualified = [r for r in results
                     if r.verdict in (Verdict.CORE, Verdict.WATCH)
                     and r.rise_probability >= self.min_rise_prob]
        qualified.sort(key=lambda x: x.final_rank, reverse=True)
        return qualified[:top_n]


# ============================================================
# 推荐报告输出
# ============================================================

def format_report(results: list[ScreenResult]) -> str:
    if not results:
        return "本轮筛选无符合条件的股票（上涨概率≥70%的标的不足）。请放宽条件或等待下一个交易日。"
    lines = ["=" * 72, f"  智能选股推荐报告  共推荐 {len(results)} 支", "=" * 72]
    for i, r in enumerate(results, 1):
        s = r.stock
        lines.append(f"\n【推荐 {i}】{s.name}({s.code})  现价 ¥{s.price:.2f}")
        lines.append(f"  判定：{r.verdict.value}  上涨概率：{r.rise_probability:.1f}%  综合排序：{r.final_rank:.2f}")
        lines.append(f"  基本面打分：{r.total_bcd_score:.2f}/10  资金打分：{r.capital_total_score:.2f}/10  量价分：{r.pv_score:.1f}/34")
        lines.append(f"  低位反弹信号：{r.low_rebound_signals}项  风险命中：{r.risk_hits}项")
        lines.append(f"  距52周高点回撤：{s.drawdown_from_high:.0f}%  PE分位：{s.pe_5y_percentile:.0f}%  股息率：{s.dividend_yield:.2f}%")
        lines.append(f"  量比：{s.vol_ratio:.2f}  换手：{s.turnover_today:.1f}%  主力净流入占比：{s.main_inflow_ratio:.1f}%")
        if r.low_rebound_signals >= 3:
            hits = LowReboundChecker.check(s)
            lines.append(f"  触发信号：{'、'.join(hits)}")
        lines.append("-" * 72)
    lines.append("\n注：以上为量化筛选结果，不构成投资建议。")
    return "\n".join(lines)


# ============================================================
# 演示用：构造示例数据运行
# ============================================================

def demo():
    """构造若干示例股票验证引擎流水线"""
    import random
    random.seed(42)
    names = ["东方科技", "蓝海药业", "金诚机械", "盛元电子", "华通电气",
             "瑞达材料", "中恒环保", "天朗通讯", "宏达精密", "新业化学",
             "远光软件", "长青股份", "海纳科技", "万和电气", "中科智能"]
    stocks = []
    for i, name in enumerate(names):
        code = f"{600000 + i:06d}"
        s = Stock(
            code=code, name=name,
            price=round(random.uniform(8, 39.5), 2),
            is_st=False, list_years=random.uniform(3.5, 15),
            total_mv=round(random.uniform(60, 480), 1),
            avg_turnover_3m=round(random.uniform(0.6, 5), 2),
            audit_qualified_3y=True, no_investigation=True, no_major_lawsuit=True,
            goodwill_to_nav=round(random.uniform(0, 28), 1),
            debt_ratio=round(random.uniform(25, 58), 1),
            ocf_3y_sum=round(random.uniform(2, 30), 1),
            net_profit_3y_sum=round(random.uniform(1.5, 25), 1),
            roe_3y_avg=round(random.uniform(8, 22), 1),
            roe_min=round(random.uniform(6, 18), 1),
            equity_multiplier=round(random.uniform(1.2, 2.4), 2),
            gross_margin=round(random.uniform(20, 45), 1),
            gross_margin_volatility=round(random.uniform(1, 6), 1),
            gross_margin_vs_industry=round(random.uniform(28, 38), 1),
            net_margin=round(random.uniform(5, 18), 1),
            roic=round(random.uniform(6, 16), 1),
            rev_cagr_3y=round(random.uniform(5, 25), 1),
            adj_profit_cagr_3y=round(random.uniform(6, 28), 1),
            ar_to_rev=round(random.uniform(10, 45), 1),
            inventory_turnover_days=round(random.uniform(40, 90), 1),
            industry_inv_turnover_days=round(random.uniform(50, 85), 1),
            cash_to_rev_ratio=round(random.uniform(0.8, 1.3), 2),
            ocf_to_np=round(random.uniform(0.6, 1.4), 2),
            pledge_ratio=round(random.uniform(0, 35), 1),
            no_major_reduction=random.random() > 0.3,
            rev_yoy_last2q=round(random.uniform(5, 30), 1),
            rev_yoy_prev=round(random.uniform(3, 20), 1),
            adj_profit_yoy_last2q=round(random.uniform(5, 35), 1),
            adj_profit_yoy_prev=round(random.uniform(2, 25), 1),
            industry_up=random.random() > 0.4,
            policy_support=random.random() > 0.5,
            rd_ratio=round(random.uniform(2, 12), 1),
            is_tech="科技" in name or "电子" in name or "软件" in name or "智能" in name,
            pe_ttm=round(random.uniform(12, 40), 1),
            pe_industry_median=round(random.uniform(20, 45), 1),
            pe_5y_percentile=round(random.uniform(5, 60), 1),
            peg=round(random.uniform(0.5, 1.5), 2),
            pb=round(random.uniform(1.2, 4.5), 2),
            dividend_yield=round(random.uniform(0.5, 5), 2),
            fcf_to_mv=round(random.uniform(2, 8), 1),
            high_52w=round(random.uniform(s:=random.uniform(10, 45), s * 1.2), 2),
            drawdown_from_high=round(random.uniform(10, 45), 1),
            ma20=round(random.uniform(0, 0), 2),
            ma60=round(random.uniform(0, 0), 2),
            ma20_slope_up=random.random() > 0.3,
            rank_20d_in_industry=round(random.uniform(40, 95), 1),
            breakthrough_vol_multiple=round(random.uniform(1, 2.5), 2),
            upper_shadow_to_body=round(random.uniform(0.3, 2.5), 2),
            pullback_vol_ratio=round(random.uniform(0.3, 0.8), 2),
            ma_converge=random.random() > 0.6,
            turnover_today=round(random.uniform(2, 18), 1),
            has_gap_up_unfilled=random.random() > 0.7,
            consecutive_limit_up=random.randint(0, 2),
            vol_ratio=round(random.uniform(1, 3.5), 2),
            main_inflow=round(random.uniform(-1, 3), 2),
            main_inflow_ratio=round(random.uniform(-2, 12), 1),
            cum_inflow_5d=round(random.uniform(-1, 5), 2),
            inflow_days_ratio=round(random.uniform(0.3, 0.9), 2),
            big_order_healthy=random.random() > 0.4,
            dragon_tiger_inst=random.randint(0, 3),
            north_holding_up_days=random.randint(0, 5),
            margin_change_pct=round(random.uniform(0, 12), 1),
            sector_inflow_rank=random.randint(1, 15),
            drawdown_from_history_high=round(random.uniform(30, 65), 1),
            price_percentile_3y=round(random.uniform(3, 40), 1),
            pe_5y_percentile_low=round(random.uniform(3, 30), 1),
            shareholder_count_down_2q=random.random() > 0.5,
            inst_holding_low=random.random() > 0.5,
            ground_volume_days=random.randint(0, 8),
            bottom_surge_vol_multiple=round(random.uniform(1, 3), 2),
            low_elevated=random.random() > 0.6,
            macd_divergence=random.random() > 0.6,
            bad_news_exhausted=random.random() > 0.7,
            unlock_ratio_3m=round(random.uniform(0, 8), 1),
            has_earning_warning=False, has_inquiry_letter=False, has_new_lawsuit=False,
        )
        # 修正 ma 使 price > ma20 > ma60 成立概率更高
        s.ma20 = round(s.price * random.uniform(0.96, 1.02), 2)
        s.ma60 = round(s.ma20 * random.uniform(0.97, 1.0), 2)
        stocks.append(s)

    engine = StockScreener(style=STYLE_QUALITY, max_price=40, max_consecutive_limit=2, min_rise_prob=70)
    results = engine.screen_batch(stocks, top_n=10)
    print(format_report(results))
    print(f"\n输入股票数：{len(stocks)}  通过硬条件：{sum(1 for s in stocks if HardFilter.check(s)[0])}  最终推荐：{len(results)}")


if __name__ == "__main__":
    demo()

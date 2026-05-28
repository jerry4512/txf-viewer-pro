"""
integrated_strategy.py
──────────────────────────────────────────────────────────────────────────────
整合 tomorrow_strategy（主決策）與 screener 籌碼資料（輔助評分）的單一選股系統。

核心原則：
  tomorrow_strategy 有否決權：大盤狀態、個股結構、買點位置、風報比
  screener 只有加分權：法人籌碼、產業共振、流動性

五個最終分類：
  buy_candidates       明日可買
  high_priority_watch  高優先觀察
  wait_pullback        等回測（強勢但不能追高）
  other_watch          其他觀察
  excluded             排除
"""

import os
import sqlite3
import pandas as pd
from datetime import datetime
from collections import defaultdict

import tomorrow_strategy as _ts

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DB_PATH  = os.path.join(_BASE_DIR, "stock_cache.db")


# ── DB 工具 ───────────────────────────────────────────────────────────────────

def _get_conn():
    conn = sqlite3.connect(_DB_PATH, timeout=60.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return conn


# ── Step 2：從 DB 取得籌碼資料 ─────────────────────────────────────────────────

def _get_chip_data(codes: list) -> dict:
    """對指定股票代號查詢近5日三大法人買超與連續買超天數。"""
    if not codes:
        return {}

    conn = _get_conn()
    try:
        ph = ','.join('?' * len(codes))
        # 近5日合計
        df5 = pd.read_sql_query(
            f"""
            SELECT code,
                   SUM(foreign_buy)    AS foreign_5d,
                   SUM(investment_buy) AS trust_5d,
                   SUM(dealer_buy)     AS dealer_5d,
                   SUM(foreign_buy + investment_buy + dealer_buy) AS total_5d
            FROM institutional_trading
            WHERE code IN ({ph})
              AND date IN (
                  SELECT DISTINCT date FROM institutional_trading
                  ORDER BY date DESC LIMIT 5
              )
            GROUP BY code
            """,
            conn, params=codes,
        )
        # 近10日明細（用於計算連續買超天數）
        df10 = pd.read_sql_query(
            f"""
            SELECT code, date, foreign_buy, investment_buy
            FROM institutional_trading
            WHERE code IN ({ph})
            ORDER BY code, date ASC
            """,
            conn, params=codes,
        )
    except Exception as e:
        print(f"[IntegratedStrategy] chip data query error: {e}")
        return {}
    finally:
        conn.close()

    result: dict = {}

    if not df5.empty:
        for _, row in df5.iterrows():
            result[row['code']] = {
                'foreign_5d':        int(row['foreign_5d']  or 0),
                'trust_5d':          int(row['trust_5d']    or 0),
                'dealer_5d':         int(row['dealer_5d']   or 0),
                'total_5d':          int(row['total_5d']    or 0),
                'foreign_consecutive': 0,
                'trust_consecutive':   0,
            }

    if not df10.empty:
        for code, grp in df10.groupby('code'):
            tail10   = grp.tail(10).to_dict('records')
            f_strike = 0
            t_strike = 0
            for r in reversed(tail10):
                if r['investment_buy'] > 0:
                    t_strike += 1
                else:
                    break
            for r in reversed(tail10):
                if r['foreign_buy'] > 0:
                    f_strike += 1
                else:
                    break
            if code not in result:
                result[code] = {
                    'foreign_5d': 0, 'trust_5d': 0,
                    'dealer_5d': 0, 'total_5d': 0,
                }
            result[code]['foreign_consecutive'] = f_strike
            result[code]['trust_consecutive']   = t_strike

    return result


def _empty_chip() -> dict:
    return {
        'foreign_5d': 0, 'trust_5d': 0, 'dealer_5d': 0,
        'total_5d': 0, 'foreign_consecutive': 0, 'trust_consecutive': 0,
    }


def _chip_tier(chip: dict) -> str:
    f  = chip.get('foreign_consecutive', 0)
    t  = chip.get('trust_consecutive',   0)
    f5 = chip.get('foreign_5d', 0)
    t5 = chip.get('trust_5d',   0)
    d5 = chip.get('dealer_5d',  0)
    if t > 0 and f > 0 and f5 > 0 and t5 > 0 and d5 > 0:
        return "黃金滿貫"
    if t > 0 and f > 0:
        return "強勢雙雄"
    if t > 0:
        return "投信鎖碼"
    if f > 0:
        return "外資鎖碼"
    return "主力佈局"


def _chip_status_label(chip: dict) -> str:
    f5  = chip.get('foreign_5d', 0)
    t5  = chip.get('trust_5d',   0)
    tot = chip.get('total_5d',   0)
    fc  = chip.get('foreign_consecutive', 0)
    tc  = chip.get('trust_consecutive',   0)
    if tot <= 0:
        return "無法人買超"
    parts = []
    if f5 > 0 and t5 > 0:
        parts.append("外資+投信同步")
    elif f5 > 0:
        parts.append(f"外資+{f5}張")
    elif t5 > 0:
        parts.append(f"投信+{t5}張")
    if tc >= 3:
        parts.append(f"投信連買{tc}日")
    if fc >= 3:
        parts.append(f"外資連買{fc}日")
    return "、".join(parts) if parts else f"三大法人+{tot}張"


# ── Step 4：產業排行（基於整合後的技術分數與籌碼加分）─────────────────────────

def _compute_industry_rankings(stocks: list) -> None:
    """
    為每檔股票標注 industry_score、industry_status、has_industry_resonance。
    直接修改傳入的 dict list（in-place）。
    """
    groups: dict = defaultdict(list)
    for s in stocks:
        ind = (s.get('industry') or '').strip() or '其他'
        groups[ind].append(s)

    for ind_name, ind_stocks in groups.items():
        n              = len(ind_stocks)
        avg_base       = sum(s.get('base_score_raw', 0) for s in ind_stocks) / n
        avg_chip_bonus = sum(s.get('chip_bonus_raw', 0) for s in ind_stocks) / n
        a_count        = sum(1 for s in ind_stocks if s.get('grade') == 'A')
        overheat_count = sum(1 for s in ind_stocks
                             if s.get('high_vol_upper_shadow')
                             or (s.get('dist_cost20_pct') or 0) > 12)

        grade_score  = (a_count  / n) * 100
        chip_norm    = min(100.0, (avg_chip_bonus / 15) * 100)
        overheat_pen = (overheat_count / n) * 100

        industry_score = round(max(0.0, min(100.0,
            avg_base   * 0.50
            + chip_norm  * 0.25
            + grade_score * 0.15
            - overheat_pen * 0.10
        )), 1)

        if overheat_count / n >= 0.30:
            status = "過熱警戒"
        elif industry_score >= 90 and n >= 3:
            status = "強勢主流"
        elif industry_score >= 80:
            status = "健康偏強"
        elif industry_score >= 70:
            status = "回測機會"
        elif industry_score >= 60:
            status = "中性觀察"
        else:
            status = "弱勢產業"

        for s in ind_stocks:
            s['industry_score']         = industry_score
            s['industry_status']        = status
            s['has_industry_resonance'] = (
                s.get('base_score_raw', 0) >= 60 and industry_score >= 80
            )


# ── Step 5：final_score 計算 ───────────────────────────────────────────────────

def _calculate_final_score(stock: dict) -> dict:
    """
    final_score = base_score + chip_bonus + industry_bonus + liquidity_bonus - risk_penalty
    """
    base_score_raw = stock.get('base_score_raw', 0)
    base_score     = round(base_score_raw * 0.65)   # 65% 權重，上限約 65

    # ── chip_bonus（上限 15）────────────────────────────────────────────
    chip_bonus = 0
    tot5 = stock.get('institution_5d_total', 0)
    f5   = stock.get('foreign_5d', 0)
    t5   = stock.get('trust_5d',   0)
    fc   = stock.get('foreign_consecutive', 0)
    tc   = stock.get('trust_consecutive',   0)
    tier = stock.get('chip_tier', '')

    if tot5 > 0:        chip_bonus += 3   # 三大法人合計買超
    if f5  > 0:         chip_bonus += 2   # 外資買超
    if t5  > 0:         chip_bonus += 3   # 投信買超
    if f5  > 0 and t5 > 0:
                        chip_bonus += 4   # 外資 + 投信同步
    if tc  >= 3:        chip_bonus += 3   # 投信連買 3 日以上
    if fc  >= 3:        chip_bonus += 2   # 外資連買 3 日以上
    if tier == '黃金滿貫':
                        chip_bonus += 5   # 黃金滿貫
    chip_bonus = min(15, chip_bonus)
    stock['chip_bonus_raw'] = chip_bonus  # 供產業排行用

    # ── industry_bonus（上限 12，最低 -5）───────────────────────────────
    ind_score   = stock.get('industry_score',  0)
    ind_status  = stock.get('industry_status', '')
    has_res     = stock.get('has_industry_resonance', False)
    ind_bonus   = 0
    if   ind_score >= 85: ind_bonus += 5
    elif ind_score >= 80: ind_bonus += 4
    elif ind_score >= 70: ind_bonus += 2
    if has_res:           ind_bonus += 5
    if ind_status == '強勢主流':    ind_bonus += 3
    elif ind_status == '過熱警戒':  ind_bonus -= 5
    ind_bonus = min(12, max(-5, ind_bonus))

    # ── liquidity_bonus（上限 8）────────────────────────────────────────
    amt_ma20  = stock.get('amount_ma20', 0) or 0
    vol_ma20  = stock.get('volume_ma20', 0) or 0
    liq_bonus = 0
    if   amt_ma20 >= 300_000_000: liq_bonus += 5
    elif amt_ma20 >= 100_000_000: liq_bonus += 4
    elif amt_ma20 >=  50_000_000: liq_bonus += 2
    if   vol_ma20 >= 3000:        liq_bonus += 3
    elif vol_ma20 >= 1000:        liq_bonus += 1
    liq_bonus = min(8, liq_bonus)

    # ── risk_penalty ────────────────────────────────────────────────────
    risk_penalty  = 0
    dist_cost20   = stock.get('dist_cost20_pct', 0)
    rr            = stock.get('risk_reward')

    if dist_cost20 > 8.0:                            risk_penalty += 8
    if stock.get('high_vol_upper_shadow', False):     risk_penalty += 8
    if rr is not None and rr < 1.5:                  risk_penalty += 6
    if rr is not None and rr < 1.0:                  risk_penalty += 14

    final_score = base_score + chip_bonus + ind_bonus + liq_bonus - risk_penalty
    final_score = max(0, min(100, final_score))

    return {
        'base_score':      base_score,
        'chip_bonus':      chip_bonus,
        'industry_bonus':  ind_bonus,
        'liquidity_bonus': liq_bonus,
        'risk_penalty':    risk_penalty,
        'final_score':     final_score,
    }


# ── Step 6：最終分類 ──────────────────────────────────────────────────────────

def _classify_final_category(stock: dict, market_regime: dict) -> str:
    """
    tomorrow_strategy 有否決權；screener 只能加分，不能升格可買。
    """
    ts_cat       = stock.get('tomorrow_category', '排除')
    chip_bonus   = stock.get('chip_bonus', 0)
    ind_bonus    = stock.get('industry_bonus', 0)
    dist_cost20  = stock.get('dist_cost20_pct', 0)
    grade        = stock.get('grade', 'C')
    rr           = stock.get('risk_reward')
    is_near_high = stock.get('is_near_60d_high', False)

    close      = stock.get('close', 0)
    stop_price = stock.get('stop_price', 0)
    stop_pct   = abs((stop_price - close) / close * 100) if close > 0 and stop_price > 0 else 0

    # ── 硬排除（tomorrow_strategy 的否決已在此體現）────────────────────
    if ts_cat == '排除':
        return 'excluded'

    # ── 明日可買：tomorrow_strategy 已批准 ───────────────────────────
    if ts_cat == '明日可買':
        return 'buy_candidates'

    # ── 等回測：籌碼強但技術位置不允許追價 ──────────────────────────
    is_extended      = (dist_cost20 > 8.0
                        or stop_pct  > 8.0
                        or (is_near_high and dist_cost20 > 5.0))
    has_strong_chips = chip_bonus >= 6 or (chip_bonus + ind_bonus) >= 10

    if is_extended and has_strong_chips and grade in ('A', 'B1'):
        return 'wait_pullback'

    # ── 高優先觀察：tomorrow_strategy 評定 ──────────────────────────
    if ts_cat == '高優先觀察':
        return 'high_priority_watch'

    # ── 其他觀察 ────────────────────────────────────────────────────
    return 'other_watch'


# ── Step 7：文案生成 ──────────────────────────────────────────────────────────

def _build_final_reason(stock: dict) -> str:
    parts = []
    regime_label  = stock.get('market_regime_label', '')
    grade_label   = stock.get('grade_label', '')
    dist          = stock.get('dist_cost20_pct', 0)
    macd_st       = stock.get('macd_status', '')
    rr            = stock.get('risk_reward')
    chip_bonus    = stock.get('chip_bonus', 0)
    tier          = stock.get('chip_tier', '')
    has_res       = stock.get('has_industry_resonance', False)

    if regime_label:   parts.append(f"大盤{regime_label}")
    if grade_label:    parts.append(f"個股{grade_label}")
    if dist:           parts.append(f"距cost20 {dist:+.1f}%")
    if macd_st:        parts.append(f"MACD {macd_st}")
    if rr is not None: parts.append(f"風報比 {rr:.1f}")
    if chip_bonus >= 8 and tier:
        parts.append(f"籌碼{tier}")
    if has_res:        parts.append("產業共振")
    return "；".join(parts)


def _build_action_suggestion(stock: dict) -> str:
    category     = stock.get('final_category', '')
    grade        = stock.get('grade', '')
    grade_label  = stock.get('grade_label', '')
    dist         = stock.get('dist_cost20_pct', 0)
    macd_st      = stock.get('macd_status', '')
    rr           = stock.get('risk_reward')
    stop_price   = stock.get('stop_price', 0)
    close        = stock.get('close', 0)
    regime_label = stock.get('market_regime_label', '')
    chip_bonus   = stock.get('chip_bonus', 0)
    has_res      = stock.get('has_industry_resonance', False)
    tier         = stock.get('chip_tier', '')
    excl_reasons = stock.get('exclude_reasons', [])

    if category == 'buy_candidates':
        parts = []
        if regime_label:   parts.append(f"大盤為{regime_label}")
        if grade:          parts.append(f"個股{grade}級")
        parts.append(f"距cost20 {dist:+.1f}%")
        if macd_st:        parts.append(f"MACD {macd_st}")
        if rr is not None: parts.append(f"風報比 {rr:.1f}")
        if chip_bonus >= 6:parts.append("法人近5日買超")
        if has_res:        parts.append("產業共振")
        if stop_price > 0: parts.append(f"停損 {stop_price:.2f}")
        return "，".join(parts) + "。可列入明日優先觀察進場。"

    elif category == 'high_priority_watch':
        if grade == 'A':
            base = f"個股結構仍為A級，距cost20 {dist:+.1f}%"
        else:
            base = f"個股{grade_label or grade + '級'}"
        if macd_st == '負柱收斂':
            ext = "MACD負柱收斂轉強中"
        elif macd_st in ('正柱放大', '正柱'):
            ext = "MACD動能偏強，等待回測確認"
        else:
            ext = f"MACD尚未完全收斂（{macd_st}）"
        return f"{base}；{ext}。等待轉強K或量縮回測確認。"

    elif category == 'wait_pullback':
        parts = []
        if chip_bonus >= 6 and tier:
            parts.append(f"籌碼條件佳（{tier}）")
        elif has_res:
            parts.append("產業共振明顯")
        else:
            parts.append("法人或產業動能尚可")
        parts.append(f"但距cost20 {dist:+.1f}%偏遠，不建議追高")
        if close > 0 and stop_price > 0:
            sl_pct = abs((stop_price - close) / close * 100)
            if sl_pct > 6:
                parts.append(f"停損距離 {sl_pct:.1f}%偏大")
        return "；".join(parts) + "。等待回測成本線後評估進場。"

    elif category == 'excluded':
        if excl_reasons:
            return "排除原因：" + "；".join(str(r) for r in excl_reasons[:2])
        return "不符合任何進場條件，排除。"

    else:  # other_watch
        if grade == 'B1':
            return f"守住60日成本線，等待站回20日成本線後確認。"
        elif grade == 'A':
            return f"個股A級，{macd_st}，持續觀察等待進場條件成熟。"
        return "技術或籌碼動能尚未達最佳可交易狀態，場外觀望。"


# ── 主函式 ────────────────────────────────────────────────────────────────────

def run_integrated_strategy(data_date: str = None) -> dict:
    """
    整合選股主入口。
    Returns:
    {
        "status":               "success",
        "data_date":            "YYYY-MM-DD",
        "market_regime":        {...},
        "summary":              {...},
        "buy_candidates":       [...],
        "high_priority_watch":  [...],
        "wait_pullback":        [...],
        "other_watch":          [...],
        "excluded":             [...],
    }
    """
    print("[IntegratedStrategy] 開始執行整合選股…")

    # ── Step 1：執行 tomorrow_strategy 取得主決策結果 ───────────────────
    ts_result     = _ts.run_tomorrow_strategy(data_date=data_date)
    market_regime = ts_result.get('market_regime', {})
    data_date     = ts_result.get('data_date', datetime.now().strftime('%Y-%m-%d'))
    regime_status = market_regime.get('status', '')
    regime_label  = market_regime.get('label', '')

    # 非 excluded 的股票（含 buy / high_watch / other_watch）
    ts_active = (
        ts_result.get('buy_candidates',      [])
        + ts_result.get('high_priority_watch', [])
        + ts_result.get('other_watch',         [])
    )
    all_codes = list({s['symbol'] for s in ts_active})

    # ── Step 2：取得籌碼資料 ─────────────────────────────────────────────
    chip_data = _get_chip_data(all_codes)

    # ── Step 3：合併個股資料 ─────────────────────────────────────────────
    enriched: list = []

    for s in ts_active:
        code = s['symbol']
        chip = chip_data.get(code, _empty_chip())
        tier = _chip_tier(chip)

        close      = s.get('close', 0)
        stop_price = s.get('stop_price', 0)
        stop_pct   = round((stop_price - close) / close * 100, 2) if close > 0 else 0

        merged = {
            # ── 識別 ─────────────────────────────────────────────────────
            'stock_id':             code,
            'stock_name':           s.get('name', ''),
            'industry':             s.get('industry', ''),
            'close':                s.get('close', 0),
            'open_price':           s.get('open_price', 0),
            'high_price':           s.get('high_price', 0),
            'low_price':            s.get('low_price', 0),
            # ── 大盤 ─────────────────────────────────────────────────────
            'market_regime':        regime_status,
            'market_regime_label':  regime_label,
            # ── tomorrow_strategy 技術面 ─────────────────────────────────
            'tomorrow_category':    s.get('category', ''),
            'grade':                s.get('grade', ''),
            'stock_grade':          s.get('grade', ''),
            'grade_label':          s.get('grade_label', ''),
            'grade_color':          s.get('grade_color', ''),
            'cost20':               s.get('cost_20', 0),
            'cost60':               s.get('cost_60', 0),
            'cost20_distance':      s.get('dist_cost20_pct', 0),
            'dist_cost20_pct':      s.get('dist_cost20_pct', 0),
            'cost20_slope':         s.get('cost20_slope', 0),
            'macd_status':          s.get('macd_status', ''),
            'macd_neg_converging':  s.get('macd_neg_converging', False),
            'macd_pos_expanding':   s.get('macd_pos_expanding',  False),
            'macd_neg_expanding':   s.get('macd_neg_expanding',  False),
            'volume_status':        s.get('volume_status', ''),
            'high_vol_upper_shadow':s.get('high_vol_upper_shadow', False),
            'down_vol':             s.get('down_vol', False),
            'vol_shrinking':        s.get('vol_shrinking', False),
            'volume_ma20':          s.get('volume_ma20', 0),
            'amount_ma20':          s.get('amount_ma20', 0),
            'stop_price':           stop_price,
            'stop_loss_pct':        stop_pct,
            'resistance_price':     s.get('resistance_price', 0),
            'is_near_60d_high':     s.get('is_near_60d_high', False),
            'risk_reward':          s.get('risk_reward'),
            'rr_valid':             s.get('rr_valid', False),
            'buy_method':           s.get('buy_method', ''),
            'entry_condition':      s.get('entry_condition', ''),
            'include_reasons':      s.get('include_reasons', []),
            'exclude_reasons':      s.get('exclude_reasons', []),
            'liquidity_level':      s.get('liquidity_level', ''),
            'instrument_type':      s.get('instrument_type', ''),
            'base_score_raw':       s.get('score', 0),  # tomorrow_strategy 原始分
            # ── 籌碼 ─────────────────────────────────────────────────────
            'foreign_5d':           chip.get('foreign_5d',  0),
            'trust_5d':             chip.get('trust_5d',    0),
            'dealer_5d':            chip.get('dealer_5d',   0),
            'institution_5d_total': chip.get('total_5d',    0),
            'foreign_consecutive':  chip.get('foreign_consecutive', 0),
            'trust_consecutive':    chip.get('trust_consecutive',   0),
            'chip_tier':            tier,
            'institution_5d_status': _chip_status_label(chip),
            # 初始化（後續填入）
            'chip_bonus_raw':  0,
            'industry_score':  0,
            'industry_status': '',
            'has_industry_resonance': False,
        }
        enriched.append(merged)

    # ── Step 4：產業排行（先計算 chip_bonus_raw 以便產業評分使用）────────
    # 先跑一輪 chip_bonus 計算
    for s in enriched:
        cb = 0
        if s['institution_5d_total'] > 0: cb += 3
        if s['foreign_5d'] > 0:           cb += 2
        if s['trust_5d']   > 0:           cb += 3
        if s['foreign_5d'] > 0 and s['trust_5d'] > 0: cb += 4
        if s['trust_consecutive']   >= 3: cb += 3
        if s['foreign_consecutive'] >= 3: cb += 2
        if s['chip_tier'] == '黃金滿貫':  cb += 5
        s['chip_bonus_raw'] = min(15, cb)

    _compute_industry_rankings(enriched)

    # ── Step 5：計算 final_score ─────────────────────────────────────────
    for s in enriched:
        scores = _calculate_final_score(s)
        s.update(scores)

    # ── Step 6：最終分類 ─────────────────────────────────────────────────
    for s in enriched:
        s['final_category'] = _classify_final_category(s, market_regime)

    # ── Step 7：文案生成 ─────────────────────────────────────────────────
    for s in enriched:
        s['final_reason']      = _build_final_reason(s)
        s['action_suggestion'] = _build_action_suggestion(s)

    # ── Step 8：處理 tomorrow_strategy 排除清單 ─────────────────────────
    excluded_passthrough: list = []
    for s in ts_result.get('excluded', []):
        e = {
            'stock_id':             s.get('symbol',    ''),
            'stock_name':           s.get('name',      ''),
            'industry':             s.get('industry',  ''),
            'close':                s.get('close'),
            'grade':                s.get('grade',     '—'),
            'stock_grade':          s.get('grade',     '—'),
            'grade_label':          s.get('grade_label', '—'),
            'cost20':               s.get('cost20'),
            'cost60':               s.get('cost60'),
            'cost20_distance':      s.get('dist_cost20_pct'),
            'dist_cost20_pct':      s.get('dist_cost20_pct'),
            'macd_status':          s.get('macd_status',   ''),
            'volume_status':        s.get('volume_status', ''),
            'volume_ma20':          s.get('volume_ma20'),
            'amount_ma20':          s.get('amount_ma20'),
            'stop_price':           s.get('stop_price'),
            'risk_reward':          s.get('risk_reward'),
            'exclude_reasons':      s.get('exclude_reasons', []),
            'instrument_type':      s.get('instrument_type', ''),
            'final_category':       'excluded',
            'tomorrow_category':    '排除',
            'market_regime':        regime_status,
            'market_regime_label':  regime_label,
            'foreign_5d': 0, 'trust_5d': 0, 'dealer_5d': 0,
            'institution_5d_total': 0, 'chip_tier': '',
            'institution_5d_status': '',
            'industry_score': 0, 'industry_status': '', 'has_industry_resonance': False,
            'base_score': 0, 'chip_bonus': 0, 'industry_bonus': 0,
            'liquidity_bonus': 0, 'risk_penalty': 0, 'final_score': 0,
        }
        reasons = s.get('exclude_reasons', [])
        e['final_reason']      = '；'.join(str(r) for r in reasons[:2])
        e['action_suggestion'] = '排除原因：' + '；'.join(str(r) for r in reasons[:2])
        excluded_passthrough.append(e)

    # ── Step 9：分桶 ─────────────────────────────────────────────────────
    buy_candidates      = []
    high_priority_watch = []
    wait_pullback       = []
    other_watch         = []
    extra_excluded      = []

    for s in enriched:
        cat = s['final_category']
        if   cat == 'buy_candidates':      buy_candidates.append(s)
        elif cat == 'high_priority_watch': high_priority_watch.append(s)
        elif cat == 'wait_pullback':       wait_pullback.append(s)
        elif cat == 'other_watch':         other_watch.append(s)
        else:                              extra_excluded.append(s)

    all_excluded = extra_excluded + excluded_passthrough

    # ── Step 10：排序 ────────────────────────────────────────────────────
    _grade_sort = {'A': 0, 'B1': 1, 'B2': 2, 'C': 3}
    _macd_sort  = {'負柱收斂': 0, '正柱放大': 1, '正柱': 2,
                   '正柱收斂': 3, '負柱': 4, '負柱擴大': 5}

    buy_candidates.sort(key=lambda x: (
        -x.get('final_score', 0),
        0 if x.get('has_industry_resonance') else 1,
        -x.get('chip_bonus', 0),
        -(x.get('risk_reward') or 0),
        abs(x.get('dist_cost20_pct', 0)),
        -(x.get('amount_ma20') or 0),
    ))

    high_priority_watch.sort(key=lambda x: (
        _grade_sort.get(x.get('stock_grade', ''), 9),
        abs(x.get('dist_cost20_pct', 0)),
        _macd_sort.get(x.get('macd_status', ''), 9),
        -x.get('final_score', 0),
        -x.get('industry_score', 0),
    ))

    wait_pullback.sort(key=lambda x: (
        -x.get('industry_score', 0),
        -x.get('chip_bonus', 0),
        -x.get('final_score', 0),
        abs(x.get('dist_cost20_pct', 0)),
    ))

    other_watch.sort(key=lambda x: (
        _grade_sort.get(x.get('stock_grade', ''), 9),
        -x.get('final_score', 0),
        abs(x.get('dist_cost20_pct', 0)),
    ))

    all_excluded.sort(key=lambda x: -x.get('final_score', 0))

    # ── 加 rank ──────────────────────────────────────────────────────────
    for i, s in enumerate(buy_candidates,      1): s['rank'] = i
    for i, s in enumerate(high_priority_watch, 1): s['rank'] = i
    for i, s in enumerate(wait_pullback,       1): s['rank'] = i
    for i, s in enumerate(other_watch,         1): s['rank'] = i

    summary = {
        'total_analyzed':            len(enriched) + len(excluded_passthrough),
        'buy_count':                 len(buy_candidates),
        'high_priority_watch_count': len(high_priority_watch),
        'wait_pullback_count':       len(wait_pullback),
        'other_watch_count':         len(other_watch),
        'excluded_count':            len(all_excluded),
    }
    print(
        f"[IntegratedStrategy] 完成 — "
        f"可買:{summary['buy_count']} "
        f"高優先:{summary['high_priority_watch_count']} "
        f"等回測:{summary['wait_pullback_count']} "
        f"其他:{summary['other_watch_count']} "
        f"排除:{summary['excluded_count']}"
    )

    return {
        'status':              'success',
        'data_date':           data_date,
        'market_regime':       market_regime,
        'summary':             summary,
        'buy_candidates':      buy_candidates,
        'high_priority_watch': high_priority_watch,
        'wait_pullback':       wait_pullback,
        'other_watch':         other_watch,
        'excluded':            all_excluded,
    }

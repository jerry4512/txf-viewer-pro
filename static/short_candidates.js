(() => {
    'use strict';

    const state = {
        initialized: false,
        loaded: false,
        loading: false,
        dashboard: null,
        progressTimer: null,
    };

    const el = id => document.getElementById(id);
    const escapeHTML = value => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');

    function number(value, digits = 1) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed.toFixed(digits) : '—';
    }

    function integer(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? Math.round(parsed).toLocaleString('zh-TW') : '—';
    }

    function percent(value, digits = 1, signed = true) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) return '—';
        return `${signed && parsed > 0 ? '+' : ''}${parsed.toFixed(digits)}%`;
    }

    function fractionPercent(value, digits = 0) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? `${(parsed * 100).toFixed(digits)}%` : '—';
    }

    function signedClass(value) {
        const parsed = Number(value);
        if (!Number.isFinite(parsed) || Math.abs(parsed) < 1e-9) return 'short-neutral';
        return parsed > 0 ? 'short-positive' : 'short-negative';
    }

    async function fetchJSON(url, options) {
        const response = await fetch(url, { cache: 'no-store', ...(options || {}) });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || body.message || `HTTP ${response.status}`);
        return body;
    }

    function showAlert(messages, warning = false) {
        const alert = el('short-alert');
        if (!alert) return;
        const list = Array.isArray(messages) ? messages.filter(Boolean) : [messages].filter(Boolean);
        if (!list.length) {
            alert.hidden = true;
            alert.textContent = '';
            return;
        }
        alert.hidden = false;
        alert.classList.toggle('is-warning', warning);
        alert.textContent = list.join('\n');
    }

    const progressSteps = [
        ['更新今日市場資料', '正在讀取富邦 TSE／OTC Snapshot…'],
        ['補齊歷史資料', '只補本地資料不足的股票，並遵守 Historical 速率限制…'],
        ['計算全市場 Factor', '計算價格結構、量價、相對弱勢與超跌懲罰…'],
        ['建立與保存排名', '保存 raw factor、component score 與 Short Score…'],
    ];

    function setProgress(index) {
        const step = progressSteps[index % progressSteps.length];
        if (el('short-loading-title')) el('short-loading-title').textContent = step[0];
        if (el('short-loading-detail')) el('short-loading-detail').textContent = step[1];
    }

    function setLoading(loading, animate = false) {
        state.loading = loading;
        const loadingNode = el('short-loading');
        const dashboard = el('short-dashboard');
        const button = el('short-refresh-btn');
        if (loadingNode) loadingNode.hidden = !loading;
        if (dashboard) dashboard.hidden = loading || !state.dashboard;
        if (button) {
            button.disabled = loading;
            button.textContent = loading ? '更新中…' : '更新隔日放空候選';
        }
        clearInterval(state.progressTimer);
        state.progressTimer = null;
        if (loading && animate) {
            let index = 0;
            setProgress(index);
            state.progressTimer = setInterval(() => setProgress(++index), 5000);
        }
    }

    async function load() {
        showAlert('');
        setLoading(true, false);
        setProgress(3);
        try {
            state.dashboard = await fetchJSON('/api/short-candidates?limit=20');
            state.loaded = true;
            render();
        } catch (error) {
            console.error('[SHORT_V1] cached result load failed', error);
            showAlert(error.message || '隔日放空候選目前無法取得');
        } finally {
            setLoading(false);
        }
    }

    async function refresh() {
        if (state.loading) return;
        showAlert('');
        setLoading(true, true);
        try {
            state.dashboard = await fetchJSON('/api/short-candidates/refresh', { method: 'POST' });
            state.loaded = true;
            render();
        } catch (error) {
            console.error('[SHORT_V1] refresh failed', error);
            showAlert(error.message || '隔日放空候選更新失敗');
        } finally {
            setLoading(false);
        }
    }

    function render() {
        const data = state.dashboard || {};
        const summary = data.summary || {};
        const candidates = data.candidates || [];
        el('short-data-date').textContent = data.dataDate || '尚未更新';
        el('short-version').textContent = data.scannerVersion === 'short_v1' ? 'Short V1' : (data.scannerVersion || '—');
        el('short-last-updated').textContent = data.lastUpdated
            ? new Date(data.lastUpdated).toLocaleString('zh-TW', { hour12: false })
            : '—';
        el('short-summary-universe').textContent = integer(summary.universeTotal);
        el('short-summary-market').textContent = `上市 ${integer(summary.tseCount)}／上櫃 ${integer(summary.otcCount)}`;
        el('short-summary-success').textContent = integer(summary.successCount);
        el('short-summary-missing').textContent = `缺資料 ${integer(summary.missingDataCount)}／API Failure ${integer(summary.apiFailureCount)}`;
        el('short-summary-excluded').textContent = integer(summary.excludedCount);
        el('short-summary-excluded-detail').textContent = `無有效收盤 ${integer(summary.snapshotInvalidExcluded)}／缺歷史 ${integer(summary.missingDataCount)}／流動性 ${integer(summary.liquidityExcluded)}／異常 ${integer(summary.abnormalExcluded)}`;
        el('short-summary-valid').textContent = integer(summary.validCandidates);
        el('short-summary-chip').textContent = `籌碼缺資料 ${integer(summary.missingChipCount)}`;
        el('short-candidate-count').textContent = `${candidates.length} 檔・資料日 ${data.dataDate || '—'}`;
        const dashboard = el('short-dashboard');
        if (dashboard) dashboard.hidden = false;
        renderTable(candidates);
        showAlert(data.warnings || [], true);
    }

    function dayTradeLabel(row) {
        if (row.day_trade_status === 'not_eligible') return ['不可現沖', 'short-day-no'];
        if (row.day_trade_status === 'eligible_current_unverified_next_day') return ['目前可現沖*', 'short-day-yes'];
        return ['未確認', 'short-neutral'];
    }

    function riskLabel(row) {
        const risks = [];
        if (row.attention_status === '注意') risks.push('注意');
        if (row.disposition_status === '處置') risks.push('處置');
        if (row.security_status && !['NORMAL', 'UNKNOWN'].includes(row.security_status)) risks.push(row.security_status);
        return risks.length ? risks.join('／') : '正常';
    }

    function renderTable(rows) {
        const tbody = el('short-candidates-body');
        if (!tbody) return;
        if (!rows.length) {
            tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;padding:35px;color:#716b7c;">尚無已保存的候選結果，請按「更新隔日放空候選」。</td></tr>';
            return;
        }
        tbody.innerHTML = rows.map(row => {
            const dayTrade = dayTradeLabel(row);
            const risk = riskLabel(row);
            const penalty = Number(row.overextension_penalty || 0);
            return `<tr data-symbol="${escapeHTML(row.symbol)}">
                <td>${integer(row.rank)}</td>
                <td><span class="short-stock-main">${escapeHTML(row.name || row.symbol)}</span><span class="short-stock-code">${escapeHTML(row.symbol)}・${escapeHTML(row.market || '')}</span></td>
                <td><span class="short-score">${number(row.short_score, 1)}</span></td>
                <td class="${signedClass(row.daily_return)}">${percent(row.daily_return)}</td>
                <td class="${signedClass(row.market_relative_strength)}">${percent(row.market_relative_strength)}</td>
                <td>${Number.isFinite(Number(row.relative_volume)) ? `${number(row.relative_volume, 2)}x` : '—'}</td>
                <td>${fractionPercent(row.close_location)}</td>
                <td>${escapeHTML(row.pattern_label || '—')}</td>
                <td>${escapeHTML(row.prior_trend || '—')}</td>
                <td>${escapeHTML(row.chip_label || '籌碼缺資料')}</td>
                <td class="${penalty > 0 ? 'short-risk' : 'short-neutral'}">${penalty > 0 ? `-${number(penalty, 0)}` : '0'}</td>
                <td class="${dayTrade[1]}" title="${escapeHTML(row.day_trade_note || '')}">${dayTrade[0]}</td>
                <td class="${risk === '正常' ? 'short-neutral' : 'short-risk'}">${escapeHTML(risk)}</td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('tr[data-symbol]').forEach(row => {
            row.addEventListener('click', () => openDetail(row.dataset.symbol));
        });
    }

    function factor(label, value) {
        return `<div><span>${escapeHTML(label)}</span><strong>${value}</strong></div>`;
    }

    function scoreRow(label, value, maximum) {
        return `<div class="short-score-row"><span>${escapeHTML(label)}</span><strong>${value === null || value === undefined ? '缺資料' : `${number(value, 1)} / ${maximum}`}</strong></div>`;
    }

    function openDetail(symbol) {
        const row = state.dashboard?.candidates?.find(item => item.symbol === symbol);
        const drawer = el('short-detail-drawer');
        const overlay = el('short-detail-overlay');
        if (!row || !drawer || !overlay) return;
        el('short-detail-code').textContent = row.symbol;
        el('short-detail-name').textContent = row.name || row.symbol;
        const reasons = (row.reasons || []).map(reason => `<li>${escapeHTML(reason)}</li>`).join('');
        const content = el('short-detail-content');
        content.innerHTML = `
            <div class="short-detail-score">
                <article><span>Final Short Score</span><strong class="short-negative">${number(row.short_score, 1)}</strong></article>
                <article><span>Base Score</span><strong>${number(row.base_short_score, 1)}</strong></article>
                <article><span>Overextension</span><strong class="short-risk">-${number(row.overextension_penalty || 0, 1)}</strong></article>
                <article><span>分數資料覆蓋</span><strong>${integer(row.score_coverage_weight)}%</strong></article>
            </div>
            <div class="short-detail-callout"><strong>為什麼入選</strong><ul>${reasons}</ul></div>
            <section class="short-detail-section">
                <h3>分數拆解</h3>
                ${scoreRow('價格結構', row.price_structure_score, 35)}
                ${scoreRow('量價／K棒', row.volume_candle_score, 25)}
                ${scoreRow('相對弱勢', row.relative_weakness_score, 25)}
                ${scoreRow('籌碼', row.capital_chip_score, 15)}
                <div class="short-score-row"><span>Base Score − Overextension</span><strong>${number(row.base_short_score, 1)} − ${number(row.overextension_penalty || 0, 1)} = ${number(row.short_score, 1)}</strong></div>
            </section>
            <section class="short-detail-section">
                <h3>原始 Factor</h3>
                <div class="short-factor-grid">
                    ${factor('今日漲跌', percent(row.daily_return))}
                    ${factor('相對大盤', percent(row.market_relative_strength))}
                    ${factor('相對產業', percent(row.industry_relative_strength))}
                    ${factor('收盤位置', fractionPercent(row.close_location))}
                    ${factor('量比', Number.isFinite(Number(row.relative_volume)) ? `${number(row.relative_volume, 2)}x` : '—')}
                    ${factor('20日均量（張）', integer(row.avg_volume_20))}
                    ${factor('前20日高', number(row.prior_20d_high, 2))}
                    ${factor('上影線比例', fractionPercent(row.upper_shadow_ratio))}
                    ${factor('前期支撐', number(row.prior_support, 2))}
                    ${factor('3日報酬', percent(row.return_3d))}
                    ${factor('5日報酬', percent(row.return_5d))}
                    ${factor('10日報酬', percent(row.return_10d))}
                    ${factor('20日報酬', percent(row.return_20d))}
                    ${factor('距離5MA', percent(row.distance_5ma))}
                    ${factor('距離20MA', percent(row.distance_20ma))}
                    ${factor('ATR14', number(row.atr14, 2))}
                    ${factor('單日移動／ATR', number(row.daily_move_atr, 2))}
                    ${factor('法人5日淨額（股）', integer(row.institutional_net_5d))}
                </div>
            </section>
            <div class="short-detail-callout" style="margin-top:15px;border-left-color:#f2c56d;">
                <strong>當沖資格限制</strong><br>${escapeHTML(row.day_trade_note || '未確認')}
                <br>注意／處置：${escapeHTML(riskLabel(row))}。Scanner 只供隔日觀察，不是進場訊號。
            </div>`;
        overlay.hidden = false;
        drawer.classList.add('is-open');
        drawer.setAttribute('aria-hidden', 'false');
    }

    function closeDetail() {
        const drawer = el('short-detail-drawer');
        const overlay = el('short-detail-overlay');
        if (overlay) overlay.hidden = true;
        if (drawer) {
            drawer.classList.remove('is-open');
            drawer.setAttribute('aria-hidden', 'true');
        }
    }

    function init() {
        if (state.initialized) return;
        state.initialized = true;
        el('short-refresh-btn')?.addEventListener('click', refresh);
        el('short-detail-close')?.addEventListener('click', closeDetail);
        el('short-detail-overlay')?.addEventListener('click', closeDetail);
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape') closeDetail();
        });
    }

    window.ShortCandidatesPage = {
        show() {
            init();
            const container = el('day-trading-container');
            if (container) container.style.display = 'flex';
            if (!state.loaded) load();
        },
        hide() {
            const container = el('day-trading-container');
            if (container) container.style.display = 'none';
            closeDetail();
        },
        load,
        refresh,
    };
})();

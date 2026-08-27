(() => {
    'use strict';

    const state = {
        initialized: false,
        loaded: false,
        loading: false,
        symbol: '00981A',
        period: 5,
        dashboard: null,
        sortKey: 'convictionScore',
        sortDirection: 'desc',
        requestId: 0,
        controller: null,
    };

    const el = id => document.getElementById(id);
    const escapeHTML = value => String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');

    function formatNumber(value) {
        const number = Number(value);
        return Number.isFinite(number) ? Math.round(number).toLocaleString('zh-TW') : '—';
    }

    function formatSignedNumber(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return '—';
        return `${number > 0 ? '+' : ''}${Math.round(number).toLocaleString('zh-TW')}`;
    }

    function formatPercentFraction(value, digits = 1) {
        const number = Number(value);
        if (!Number.isFinite(number)) return '—';
        const percent = number * 100;
        return `${percent > 0 ? '+' : ''}${percent.toFixed(digits)}%`;
    }

    function formatWeight(value, digits = 2) {
        const number = Number(value);
        return Number.isFinite(number) ? `${number.toFixed(digits)}%` : '—';
    }

    function formatWeightChange(value) {
        const number = Number(value);
        if (!Number.isFinite(number)) return '—';
        return `${number > 0 ? '+' : ''}${number.toFixed(2)}pp`;
    }

    function signedClass(value) {
        const number = Number(value);
        if (!Number.isFinite(number) || Math.abs(number) < 1e-12) return 'etf-neutral';
        return number > 0 ? 'etf-positive' : 'etf-negative';
    }

    function setLoading(loading) {
        state.loading = loading;
        const loadingNode = el('etf-loading');
        const dashboard = el('etf-dashboard');
        const refresh = el('etf-refresh-btn');
        if (loadingNode) loadingNode.hidden = !loading;
        if (dashboard) dashboard.hidden = loading || !state.dashboard;
        if (refresh) {
            refresh.disabled = loading;
            refresh.textContent = loading ? '整理中…' : '↻ 重新整理';
        }
    }

    function showAlert(message, warning = false) {
        const alert = el('etf-alert');
        if (!alert) return;
        if (!message) {
            alert.hidden = true;
            alert.textContent = '';
            return;
        }
        alert.hidden = false;
        alert.classList.toggle('is-warning', warning);
        alert.textContent = message;
    }

    async function fetchJSON(url, options) {
        const response = await fetch(url, { cache: 'no-store', ...(options || {}) });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || body.message || `HTTP ${response.status}`);
        return body;
    }

    async function load({ force = false } = {}) {
        const requestId = ++state.requestId;
        state.controller?.abort();
        const controller = new AbortController();
        state.controller = controller;
        showAlert('');
        setLoading(true);
        try {
            const url = `/api/etf/holdings?symbol=${encodeURIComponent(state.symbol)}`
                + `&period=${state.period}&refresh=${force ? 'true' : 'false'}`;
            const dashboard = await fetchJSON(url, { signal: controller.signal });
            if (requestId !== state.requestId) return;
            state.dashboard = dashboard;
            state.loaded = true;
            renderDashboard();
            if (state.dashboard.refreshWarning) showAlert(state.dashboard.refreshWarning, true);
        } catch (error) {
            if (error.name === 'AbortError') return;
            console.error('[ETF] 動向載入失敗', error);
            showAlert(error.message || 'ETF 持股資料目前無法取得');
        } finally {
            if (requestId === state.requestId) setLoading(false);
        }
    }

    async function refresh() {
        const requestId = ++state.requestId;
        state.controller?.abort();
        const controller = new AbortController();
        state.controller = controller;
        showAlert('');
        setLoading(true);
        try {
            const dashboard = await fetchJSON('/api/etf/holdings/refresh', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ symbol: state.symbol, period: state.period }),
                signal: controller.signal,
            });
            if (requestId !== state.requestId) return;
            state.dashboard = dashboard;
            state.loaded = true;
            renderDashboard();
        } catch (error) {
            if (error.name === 'AbortError') return;
            console.error('[ETF] holdings refresh failed', error);
            showAlert(error.message || 'ETF 持股資料目前無法取得');
        } finally {
            if (requestId === state.requestId) setLoading(false);
        }
    }

    function renderDashboard() {
        const data = state.dashboard;
        if (!data) return;
        const dashboard = el('etf-dashboard');
        if (dashboard) dashboard.hidden = false;
        el('etf-data-date').textContent = data.dataDate || '—';
        el('etf-last-updated').textContent = data.lastUpdated
            ? new Date(data.lastUpdated).toLocaleString('zh-TW', { hour12: false })
            : '—';
        const summary = data.summary || {};
        const period = Number(data.selectedPeriod) || state.period;
        const periodLabel = period === 1 ? '今日' : `${period}日`;
        const windowInfo = data.analysisWindow || {};
        el('etf-summary-new').textContent = summary.newPositions ?? 0;
        el('etf-summary-add').textContent = summary.activeAdds ?? 0;
        el('etf-summary-reduce').textContent = summary.activeReduces ?? 0;
        el('etf-summary-fast').textContent = summary.fastAccumulations ?? 0;
        el('etf-summary-fast-label').textContent = `${periodLabel}快速建倉`;
        el('etf-baseline-chip').textContent = `申贖縮放基準 ${formatPercentFraction(summary.fundScalingBaseline)}`
            + `・樣本 ${summary.baselineSampleSize ?? 0}`;
        el('etf-holdings-count').textContent = `${(data.holdings || []).length} 檔・使用 ${windowInfo.usedDisclosureDateCount ?? 0} 個揭露日`;
        el('etf-period-description').textContent = `目前依最近 ${windowInfo.usedDisclosureDateCount ?? period} 個 ETF 持股揭露日分析（${windowInfo.from || '—'}～${windowInfo.to || '—'}）。`;
        el('etf-period-relative-heading').textContent = `${periodLabel}相對增配`;
        el('etf-period-add-heading').textContent = `${periodLabel}加碼`;
        el('etf-period-reduce-heading').textContent = `${periodLabel}減碼`;
        el('etf-period-score-heading').textContent = `${periodLabel}分數`;
        renderIntents();
        renderTable();
    }

    function renderIntents() {
        const root = el('etf-intent-groups');
        if (!root) return;
        const definitions = [
            ['FAST_ACCUMULATION', '🔥 快速建倉'],
            ['CONVICTION_RISING', '↑ 信念上升'],
            ['CORE_HOLDING', '→ 核心持有'],
            ['CONVICTION_DECLINING', '↓ 信念下降'],
            ['TACTICAL', '⚠️ 戰術操作'],
        ];
        root.innerHTML = definitions.map(([key, label]) => {
            const stocks = state.dashboard?.intents?.[key] || [];
            const content = stocks.length
                ? stocks.slice(0, 8).map(stock => (
                    `<button class="etf-stock-pill" data-stock="${escapeHTML(stock.stockSymbol)}">`
                    + `${escapeHTML(stock.stockName || stock.stockSymbol)}</button>`
                )).join('')
                : '<span class="etf-empty">本期無訊號</span>';
            return `<article class="etf-intent-group"><h3>${label}</h3><div>${content}</div></article>`;
        }).join('');
        root.querySelectorAll('[data-stock]').forEach(button => {
            button.addEventListener('click', () => openDetail(button.dataset.stock));
        });
    }

    function sortValue(row, key) {
        if (key === 'stockName') return `${row.stockName || ''}${row.stockSymbol || ''}`;
        if (key === 'intent' || key === 'behavior') return String(row[key] || '');
        const number = Number(row[key]);
        return Number.isFinite(number) ? number : -Infinity;
    }

    function renderTable() {
        const tbody = el('etf-holdings-body');
        if (!tbody) return;
        const direction = state.sortDirection === 'asc' ? 1 : -1;
        const rows = [...(state.dashboard?.holdings || [])].sort((left, right) => {
            const a = sortValue(left, state.sortKey);
            const b = sortValue(right, state.sortKey);
            if (typeof a === 'string' || typeof b === 'string') {
                return String(a).localeCompare(String(b), 'zh-Hant') * direction;
            }
            return (a - b) * direction;
        });
        tbody.innerHTML = rows.map(row => {
            const price = Number(row.currentPrice);
            const priceText = Number.isFinite(price) && price > 0 ? price.toLocaleString('zh-TW') : '—';
            return `<tr data-stock="${escapeHTML(row.stockSymbol)}">
                <td><span class="etf-stock-main">${escapeHTML(row.stockName || row.stockSymbol)}</span><span class="etf-stock-code">${escapeHTML(row.stockSymbol)}</span></td>
                <td>${escapeHTML(row.behaviorLabel)}</td>
                <td>${formatNumber(row.quantity)}</td>
                <td class="${signedClass(row.quantityChange)}">${formatSignedNumber(row.quantityChange)}</td>
                <td>${formatWeight(row.weight)}</td>
                <td class="${signedClass(row.weightChange)}">${formatWeightChange(row.weightChange)}</td>
                <td class="${signedClass(row.cumulativeRelativeAllocationChange)}">${formatPercentFraction(row.cumulativeRelativeAllocationChange)}</td>
                <td class="etf-positive">${row.activeAddCount ?? 0}</td>
                <td class="etf-negative">${row.activeReduceCount ?? 0}</td>
                <td><span class="etf-score">${row.convictionScore ?? '—'}</span></td>
                <td><span class="etf-intent-label">${escapeHTML(row.intentLabel)}</span></td>
                <td>${priceText}</td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('tr[data-stock]').forEach(row => {
            row.addEventListener('click', () => openDetail(row.dataset.stock));
        });
        document.querySelectorAll('.etf-table th[data-sort]').forEach(th => {
            th.classList.toggle('is-sorted', th.dataset.sort === state.sortKey);
        });
    }

    function metricCard(label, value, className = '') {
        return `<div class="etf-detail-card"><span>${escapeHTML(label)}</span><strong class="${className}">${value}</strong></div>`;
    }

    async function openDetail(stockSymbol) {
        const drawer = el('etf-detail-drawer');
        const overlay = el('etf-detail-overlay');
        const content = el('etf-detail-content');
        const cached = state.dashboard?.holdings?.find(row => row.stockSymbol === stockSymbol);
        if (!drawer || !overlay || !content) return;
        el('etf-detail-code').textContent = stockSymbol;
        el('etf-detail-name').textContent = cached?.stockName || '載入中…';
        content.innerHTML = '<div class="etf-loading"><span></span>載入個股持股時間序列…</div>';
        overlay.hidden = false;
        drawer.classList.add('is-open');
        drawer.setAttribute('aria-hidden', 'false');
        try {
            const detail = await fetchJSON(
                `/api/etf/holdings/${encodeURIComponent(state.symbol)}/stocks/${encodeURIComponent(stockSymbol)}?period=${state.period}`
            );
            renderDetail(detail.stock);
        } catch (error) {
            content.innerHTML = `<div class="etf-alert">${escapeHTML(error.message || '個股明細目前無法取得')}</div>`;
        }
    }

    function renderDetail(stock) {
        el('etf-detail-name').textContent = stock.stockName || stock.stockSymbol;
        const content = el('etf-detail-content');
        const history = [...(stock.history || [])].reverse().slice(0, 20);
        const scoreWindow = Number(stock.analysisPeriod) || state.period;
        const selectedScore = stock.convictionScore;
        const price = Number(stock.currentPrice);
        const priceValue = Number.isFinite(price) && price > 0 ? price.toLocaleString('zh-TW') : '—';
        const historyRows = history.map(row => `<tr>
            <td>${escapeHTML(row.date)}</td>
            <td>${formatNumber(row.quantity)}</td>
            <td class="${signedClass(row.quantityChange)}">${formatSignedNumber(row.quantityChange)}</td>
            <td>${formatWeight(row.weight)}</td>
            <td class="${signedClass(row.relativeAllocationChange)}">${formatPercentFraction(row.relativeAllocationChange)}</td>
            <td>${escapeHTML(row.behaviorLabel)}</td>
        </tr>`).join('');
        const scoreRows = ((stock.scoreBreakdowns || {})[String(scoreWindow)] || stock.scoreBreakdown || []).map(item => (
            `<div><span>${escapeHTML(item.rule)}</span><strong class="${signedClass(item.points)}">${item.points > 0 ? '+' : ''}${item.points}</strong></div>`
        )).join('');
        content.innerHTML = `
            <div class="etf-detail-cards">
                ${metricCard('目前持股', formatNumber(stock.quantity))}
                ${metricCard('目前權重', formatWeight(stock.weight))}
                ${metricCard('參考股價', priceValue)}
                ${metricCard(`${scoreWindow}D 信念分數`, String(selectedScore ?? '—'), 'etf-positive')}
            </div>
            <div class="etf-detail-callout"><strong>${escapeHTML(stock.intentLabel)}</strong><br>${escapeHTML(stock.intentReason)}<br><span class="etf-neutral">${escapeHTML(stock.behaviorReason)}</span></div>
            <div class="etf-detail-cards">
                ${metricCard(`${scoreWindow}D 累積相對增配`, formatPercentFraction(stock.cumulativeRelativeAllocationChange), signedClass(stock.cumulativeRelativeAllocationChange))}
                ${metricCard(`${scoreWindow}D 主動加碼`, String(stock.activeAddCount ?? 0), 'etf-positive')}
                ${metricCard(`${scoreWindow}D 主動減碼`, String(stock.activeReduceCount ?? 0), 'etf-negative')}
                ${metricCard(`${scoreWindow}D 權重變化`, formatWeightChange(stock.periodWeightChange), signedClass(stock.periodWeightChange))}
            </div>
            <section class="etf-detail-section">
                <h3>持股變化時間序列（最近 20 個揭露日）</h3>
                <div class="etf-detail-table-wrap"><table class="etf-detail-table">
                    <thead><tr><th>日期</th><th>持股</th><th>單日變化</th><th>權重</th><th>相對配置</th><th>判斷</th></tr></thead>
                    <tbody>${historyRows}</tbody>
                </table></div>
            </section>
            <section class="etf-detail-section">
                <h3>信念分數明細（透明規則）</h3>
                <div class="etf-score-list">${scoreRows || '<div>本期沒有額外加減分</div>'}</div>
            </section>
            <div class="etf-detail-callout" style="margin-top:15px;border-left-color:#f5c76b;">ETF 決定「看誰」，價格決定「什麼時候買」。本頁是候選研究，不是交易訊號。</div>`;
    }

    function closeDetail() {
        const drawer = el('etf-detail-drawer');
        const overlay = el('etf-detail-overlay');
        if (overlay) overlay.hidden = true;
        if (drawer) {
            drawer.classList.remove('is-open');
            drawer.setAttribute('aria-hidden', 'true');
        }
    }

    function init() {
        if (state.initialized) return;
        state.initialized = true;
        el('etf-refresh-btn')?.addEventListener('click', refresh);
        el('etf-symbol-selector')?.addEventListener('change', event => {
            state.symbol = event.target.value;
            state.loaded = false;
            load();
        });
        el('etf-period-switch')?.querySelectorAll('[data-period]').forEach(button => {
            button.addEventListener('click', () => {
                state.period = Number(button.dataset.period) || 5;
                el('etf-period-switch').querySelectorAll('[data-period]').forEach(item => {
                    item.classList.toggle('is-active', item === button);
                });
                load();
            });
        });
        document.querySelectorAll('.etf-table th[data-sort]').forEach(th => {
            th.addEventListener('click', () => {
                const key = th.dataset.sort;
                if (state.sortKey === key) {
                    state.sortDirection = state.sortDirection === 'desc' ? 'asc' : 'desc';
                } else {
                    state.sortKey = key;
                    state.sortDirection = key === 'stockName' ? 'asc' : 'desc';
                }
                renderTable();
            });
        });
        el('etf-detail-close')?.addEventListener('click', closeDetail);
        el('etf-detail-overlay')?.addEventListener('click', closeDetail);
        document.addEventListener('keydown', event => {
            if (event.key === 'Escape') closeDetail();
        });
    }

    window.ETFHoldingsPage = {
        show() {
            init();
            const container = el('etf-holdings-container');
            if (container) container.style.display = 'flex';
            if (!state.loaded) load();
        },
        hide() {
            const container = el('etf-holdings-container');
            if (container) container.style.display = 'none';
            closeDetail();
        },
        load,
        refresh,
    };
})();

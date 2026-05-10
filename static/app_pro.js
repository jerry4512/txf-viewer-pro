let viewMode = 1; // 1: 單圖, 2: 雙圖, 3: 三圖
let panes = [];
let globalRefPrice = 0;
let activeStockTab = 'chart'; // 'chart' or 'screener'

// 全域錯誤監控 (僅輸出到 Console，避免彈窗干擾)
window.onerror = function(msg, url, lineNo, columnNo, error) {
    const errText = '[Runtime Error] ' + msg + ' at ' + url + ':' + lineNo + ':' + columnNo;
    console.error(errText, error);
    return true; // 阻止預設彈窗
};

class TradingPane {
    constructor(id) {
        this.id = id;
        this.currentPeriod = document.getElementById(`${id}-period`).value;
        this.chart = null;
        this.candleSeries = null;
        this.maSeries = {};
        this.macdChart = null;
        this.macdSeries = {};
        this.kbarsCache = [];
        this.oldestTime = null;
        this.isSyncing = false;
        this.highOverlay = null;
        this.lowOverlay = null;
        this.pvpEnabled = true; // 預設開啟 PVP
        this.pvpData = null;
        this.pvpLines = { poc: null, vah: null, val: null };
        this.pvpCanvas = null;
        this.pvpMarked = false;
        this.timeOffset = 0; // 移除手動補償，交給瀏覽器處理
        this.isCrosshairActive = false; // 追蹤實體滑鼠是否在圖表上
        this.isSyncedFocus = false;     // 追蹤是否正在接收外部同步
        this.isMouseInside = false;
        this.isActualMouseMove = false;  // 關鍵新增：追蹤是否真的有滑鼠實體移動
        this.lastMouseMoveTime = 0;      // 關鍵新增：追蹤最後一次滑鼠移動的時間戳
        this.fullMaCaches = { 5: [], 10: [], 20: [] };
        this.fullMacdCache = { macdLine: [], signalLine: [], histogram: [] };
        this.timeToIndex = new Map();
        this.trendIndicator = null;
        this.trendBgCanvas = null;
        this.trendBgEnabled = false;
    }

    init() {
        try {
        const chartOptions = {
            layout: { textColor: '#d1d4dc', background: { type: 'solid', color: '#000000' }, fontSize: 14 },
            grid: { vertLines: { color: '#333333' }, horzLines: { color: '#333333' } },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            timeScale: { 
                timeVisible: true, 
                secondsVisible: false,
                tickMarkFormatter: (time, tickMarkType, locale) => {
                    // 顯示時才補償 8 小時
                    const d = new Date((time + 28800) * 1000);
                    const hh = String(d.getUTCHours()).padStart(2, '0');
                    const mm = String(d.getUTCMinutes()).padStart(2, '0');
                    return `${hh}:${mm}`;
                }
            },
            priceScale: { autoScale: true, borderVisible: false, alignLabels: true },
            localization: {
                timeFormatter: (ts) => {
                    // 顯示時才補償 8 小時
                    const d = new Date((ts + 28800) * 1000);
                    const y = d.getUTCFullYear();
                    const m = (d.getUTCMonth() + 1).toString().padStart(2, '0');
                    const day = d.getUTCDate().toString().padStart(2, '0');
                    const hh = d.getUTCHours().toString().padStart(2, '0');
                    const mm = d.getUTCMinutes().toString().padStart(2, '0');
                    return `${y}/${m}/${day} ${hh}:${mm}`;
                }
            }
        };

        const mainEl = document.getElementById(`${this.id}-main`);
        this.chart = LightweightCharts.createChart(mainEl, chartOptions);
        this.candleSeries = this.chart.addCandlestickSeries({
            upColor: '#FF0000', downColor: '#00FF00', borderVisible: false,
            wickUpColor: '#FFFFFF', wickDownColor: '#FFFFFF',
            priceFormat: { type: 'price', precision: 0, minMove: 1 }
        });

        this.maSeries[5] = this.chart.addLineSeries({ color: '#FFFF00', lineWidth: 2, priceFormat: { type: 'price', precision: 0 } });
        this.maSeries[10] = this.chart.addLineSeries({ color: '#00FFFF', lineWidth: 2, priceFormat: { type: 'price', precision: 0 } });
        this.maSeries[20] = this.chart.addLineSeries({ color: '#B200FF', lineWidth: 2, priceFormat: { type: 'price', precision: 0 } });

        // 新增：成交量量柱
        this.volumeSeries = this.chart.addHistogramSeries({
            priceFormat: { type: 'volume' },
            priceScaleId: 'volume-scale', // 使用獨立比例尺
        });
        this.chart.priceScale('volume-scale').applyOptions({
            scaleMargins: {
                top: 0.8, // 留出上方 80% 空間給價格
                bottom: 0,
            },
        });

        const macdEl = document.getElementById(`${this.id}-macd-chart`);
        this.macdChart = LightweightCharts.createChart(macdEl, { ...chartOptions, timeScale: { ...chartOptions.timeScale, visible: false } });
        // 建立自訂價格格式器，將實際的 -30 ~ +30 範圍，映射為 Y 軸顯示 20 ~ 80
        const macdFormatter = val => (val + 50).toFixed(0);
        this.macdSeries.line = this.macdChart.addLineSeries({ 
            color: '#F8E71C', 
            lineWidth: 2,
            priceFormat: { type: 'custom', formatter: macdFormatter, minMove: 1 }
        });
        this.macdSeries.signal = this.macdChart.addLineSeries({ 
            color: '#00E4FF', 
            lineWidth: 2,
            priceFormat: { type: 'custom', formatter: macdFormatter, minMove: 1 }
        });
        this.macdSeries.hist = this.macdChart.addHistogramSeries({ 
            color: '#26a69a',
            priceFormat: { type: 'custom', formatter: macdFormatter, minMove: 1 }
        });

        // 建立最高/最低價標籤，並賦予唯一 ID
        this.highOverlay = document.createElement('div');
        this.highOverlay.id = `${this.id}-high-marker`;
        this.highOverlay.className = 'marker-overlay high-marker';
        this.highOverlay.style.color = '#ff4444';
        
        this.lowOverlay = document.createElement('div');
        this.lowOverlay.id = `${this.id}-low-marker`;
        this.lowOverlay.className = 'marker-overlay low-marker';
        this.lowOverlay.style.color = '#44ff44';

        // 建立 PVP 畫布
        this.pvpCanvas = document.createElement('canvas');
        this.pvpCanvas.style.position = 'absolute';
        this.pvpCanvas.style.top = '0';
        this.pvpCanvas.style.left = '0';
        this.pvpCanvas.style.width = '100%';
        this.pvpCanvas.style.height = '100%';
        this.pvpCanvas.style.pointerEvents = 'none';
        this.pvpCanvas.style.zIndex = '100'; // 強制置頂
        mainEl.appendChild(this.pvpCanvas);
        
        // 初始化趨勢背景畫布 (層級低於 PVP 但高於圖表)
        this.trendBgCanvas = document.createElement('canvas');
        this.trendBgCanvas.style.position = 'absolute';
        this.trendBgCanvas.style.top = '0';
        this.trendBgCanvas.style.left = '0';
        this.trendBgCanvas.style.width = '100%';
        this.trendBgCanvas.style.height = '100%';
        this.trendBgCanvas.style.pointerEvents = 'none';
        this.trendBgCanvas.style.zIndex = '90';
        mainEl.appendChild(this.trendBgCanvas);

        // 初始化趨勢指示器 (永遠建立，由 updateTrend 控制顯隱)
        this.trendIndicator = document.createElement('div');
        this.trendIndicator.className = 'trend-indicator';
        this.trendIndicator.style.display = 'none'; // 預設隱藏
        this.trendIndicator.innerHTML = '<span class="trend-dot"></span><span class="trend-text">方向：讀取中</span>';
        mainEl.appendChild(this.trendIndicator);
        
        // 初始尺寸對齊
        setTimeout(() => {
            this.pvpCanvas.width = mainEl.clientWidth;
            this.pvpCanvas.height = mainEl.clientHeight;
            this.trendBgCanvas.width = mainEl.clientWidth;
            this.trendBgCanvas.height = mainEl.clientHeight;
        }, 500);

        mainEl.appendChild(this.highOverlay);
        mainEl.appendChild(this.lowOverlay);

        this.chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
            if (!range || this.isSyncing) return;
            this.isSyncing = true;
            try {
                this.macdChart.timeScale().setVisibleLogicalRange(range);
                this.updateMarkers(range);
                if (this.pvpEnabled) this.drawPVP();
                if (range.from < 10 && !this.isLoading && this.oldestTime) this.loadMore();

                // 跨視窗時間軸同步 (受開關控制)
                const syncEl = document.getElementById('sync-toggle-btn');
                if (syncEl?.checked && typeof panes !== 'undefined' && Array.isArray(panes)) {
                    panes.forEach(pane => {
                        if (pane && pane !== this && pane.chart && !pane.isLoading) {
                            pane.isSyncing = true;
                            pane.chart.timeScale().setVisibleLogicalRange(range);
                            pane.isSyncing = false;
                        }
                    });
                }
                // 觸發背景繪製
                this.drawTrendBg();
            } finally {
                this.isSyncing = false;
            }
        });

        this.macdChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
            if (!range || this.isSyncing) return;
            this.isSyncing = true;
            this.chart.timeScale().setVisibleLogicalRange(range);
            this.isSyncing = false;
        });

        // 實體滑鼠偵測，防止數據更新時的虛假跳轉
        const handleMouseLeave = () => {
            setTimeout(() => {
                const isHovered = mainEl.matches(':hover') || macdEl.matches(':hover');
                if (!isHovered) {
                    this.isMouseInside = false;
                    this.isCrosshairActive = false;
                    
                    // 只有在沒有接收外部同步的情況下，才重置回最新價
                    if (!this.isSyncedFocus) {
                        this.showLatestLegend();
                    }
                    
                    const syncEl = document.getElementById('sync-toggle-btn');
                    const isSyncEnabled = syncEl?.checked ?? true;
                    if (isSyncEnabled && typeof panes !== 'undefined' && Array.isArray(panes)) {
                        panes.forEach(pane => {
                            if (pane && pane !== this && pane.chart && typeof pane.clearSync === 'function') {
                                pane.clearSync();
                            }
                        });
                    }
                }
            }, 50);
        };

        const markMouseMove = () => {
            this.isActualMouseMove = true;
            this.lastMouseMoveTime = Date.now();
        };
        mainEl.addEventListener('mousemove', markMouseMove);
        macdEl.addEventListener('mousemove', markMouseMove);

        mainEl.addEventListener('mouseenter', () => { this.isMouseInside = true; });
        mainEl.addEventListener('mouseleave', handleMouseLeave);
        macdEl.addEventListener('mouseenter', () => { this.isMouseInside = true; });
        macdEl.addEventListener('mouseleave', handleMouseLeave);

        this.chart.subscribeCrosshairMove(p => {
            try {
                const exactTime = p.point ? this.chart.timeScale().coordinateToTime(p.point.x) : null;
                const isHovered = mainEl.matches(':hover') || macdEl.matches(':hover');
                
                // 本機連動：主圖 -> MACD (永遠執行)
                if (exactTime && isHovered) {
                    this.macdChart.setCrosshairPosition(undefined, exactTime, this.macdSeries.line);
                }

                // 如果是外部同步引起的移動，不執行後續連動邏輯
                if (this.isSyncing) return;
                
                // 效能優化：限制更新頻率 (約 60fps)
                const now = Date.now();
                if (this.lastMoveTime && now - this.lastMoveTime < 16) return;
                this.lastMoveTime = now;
                
                // 只有在真的是實體滑鼠在滑動時 (200毫秒內)，我們才判定為手動觸發的數據連動
                const isUserMoving = this.isActualMouseMove && (Date.now() - this.lastMouseMoveTime < 200);

                if (isHovered) {
                    if (isUserMoving && exactTime) {
                        this.isCrosshairActive = true;
                        this.isSyncedFocus = false; // 手動介入，解除外部連動標記
                        
                        // 1. 更新 Legend 數據
                        const candle = p.time ? p.seriesData.get(this.candleSeries) : null;
                        if (candle) {
                            const idx = this.timeToIndex.get(p.time);
                            let v5, v10, v20;
                            if (idx !== undefined) {
                                v5 = this.fullMaCaches[5][idx] ? this.fullMaCaches[5][idx].value : undefined;
                                v10 = this.fullMaCaches[10][idx] ? this.fullMaCaches[10][idx].value : undefined;
                                v20 = this.fullMaCaches[20][idx] ? this.fullMaCaches[20][idx].value : undefined;
                            }
                            this.updateLegend(candle, v5, v10, v20);
                        } else {
                            this.updateLegend(null);
                        }

                        // 2. 跨視窗同步 (僅在開關開啟時執行)
                        const syncEl = document.getElementById('sync-toggle-btn');
                        if (syncEl?.checked && typeof panes !== 'undefined' && Array.isArray(panes)) {
                            panes.forEach(pane => {
                                if (pane && pane !== this && pane.chart && !pane.isLoading && typeof pane.syncFromExternal === 'function') {
                                    pane.syncFromExternal(exactTime);
                                }
                            });
                        }
                    }
                    // 如果 isUserMoving 為 false，則靜態保持目前狀態，不進行任何動作，防止新 Ticks 造成準星跳動或版面重繪
                } else if (this.kbarsCache.length > 0) {
                    // 滑鼠不在 K 棒區或移出圖表
                    this.isCrosshairActive = false;
                    this.showLatestLegend();
                    
                    // 如果開啟了同步，才需要通知其他視窗清除
                    const syncEl = document.getElementById('sync-toggle-btn');
                    if (syncEl?.checked && typeof panes !== 'undefined' && Array.isArray(panes)) {
                        panes.forEach(pane => {
                            if (pane && pane !== this && pane.chart && typeof pane.clearSync === 'function') {
                                pane.clearSync();
                            }
                        });
                    }
                }
            } catch (err) {
                console.warn("滑鼠連動發生錯誤", err);
            }
        });

        this.macdChart.subscribeCrosshairMove(p => {
            if (this.isSyncing) return;
            try {
                const exactTime = p.point ? this.macdChart.timeScale().coordinateToTime(p.point.x) : null;
                const isHovered = mainEl.matches(':hover') || macdEl.matches(':hover');
                if (exactTime && isHovered) {
                    this.chart.setCrosshairPosition(undefined, exactTime, this.candleSeries);
                }
            } catch (err) {}
        });

        const storageKey = `settings-${this.id}`;
        const savedSettings = JSON.parse(localStorage.getItem(storageKey) || '{}');

        // 1. 週期記憶
        const periodEl = document.getElementById(`${this.id}-period`);
        if (savedSettings.period) {
            this.currentPeriod = savedSettings.period;
            periodEl.value = this.currentPeriod;
        }
        periodEl.addEventListener('change', (e) => {
            this.currentPeriod = e.target.value;
            const current = JSON.parse(localStorage.getItem(storageKey) || '{}');
            current.period = this.currentPeriod;
            localStorage.setItem(storageKey, JSON.stringify(current));
            this.reload();
        });

        // 2. 均線記憶
        [5, 10, 20].forEach(n => {
            const cb = document.getElementById(`${this.id}-ma${n}`);
            if (savedSettings[`ma${n}`] !== undefined) {
                cb.checked = savedSettings[`ma${n}`];
            }
            this.maSeries[n].applyOptions({ visible: cb.checked });

            cb.addEventListener('change', (e) => {
                this.maSeries[n].applyOptions({ visible: e.target.checked });
                const current = JSON.parse(localStorage.getItem(storageKey) || '{}');
                current[`ma${n}`] = e.target.checked;
                localStorage.setItem(storageKey, JSON.stringify(current));
            });
        });

        // 3. MACD 記憶
        const macdCb = document.getElementById(`${this.id}-macd`);
        if (savedSettings.macd !== undefined) {
            macdCb.checked = savedSettings.macd;
        }
        macdEl.style.display = macdCb.checked ? 'block' : 'none';

        macdCb.addEventListener('change', (e) => {
            macdEl.style.display = e.target.checked ? 'block' : 'none';
            const current = JSON.parse(localStorage.getItem(storageKey) || '{}');
            current.macd = e.target.checked;
            localStorage.setItem(storageKey, JSON.stringify(current));
            this.resize();
        });

        // 4. PVP 開關記憶
        const pvpCb = document.getElementById(`${this.id}-pvp`);
        if (savedSettings.pvp !== undefined) {
            this.pvpEnabled = savedSettings.pvp;
            pvpCb.checked = this.pvpEnabled;
        }
        pvpCb.addEventListener('change', (e) => {
            this.pvpEnabled = e.target.checked;
            const current = JSON.parse(localStorage.getItem(storageKey) || '{}');
            current.pvp = this.pvpEnabled;
            localStorage.setItem(storageKey, JSON.stringify(current));
            if (!this.pvpEnabled) {
                if (this.pvpCanvas) {
                    const ctx = this.pvpCanvas.getContext('2d');
                    ctx.clearRect(0, 0, this.pvpCanvas.width, this.pvpCanvas.height);
                }
                ['poc', 'vah', 'val'].forEach(id => {
                    if (this.pvpLines[id]) {
                        this.candleSeries.removePriceLine(this.pvpLines[id]);
                        this.pvpLines[id] = null;
                    }
                });
                // 隱藏 PVP START 箭頭
                if (this.candleSeries) this.candleSeries.setMarkers([]);
                this.pvpMarked = false; 
            } else {
                this.drawPVP();
            }
        });

        // 5. 趨勢背景開關記憶 (加入安全防護，避免 HTML 快取導致崩潰)
        const trendBgCb = document.getElementById(`${this.id}-trend-bg`);
        const trendBgWrap = document.getElementById(`${this.id}-trend-bg-wrap`);
        
        if (trendBgCb) {
            if (savedSettings.trendBg !== undefined) {
                this.trendBgEnabled = savedSettings.trendBg;
                trendBgCb.checked = this.trendBgEnabled;
            }
            trendBgCb.addEventListener('change', (e) => {
                this.trendBgEnabled = e.target.checked;
                const current = JSON.parse(localStorage.getItem(storageKey) || '{}');
                current.trendBg = this.trendBgEnabled;
                localStorage.setItem(storageKey, JSON.stringify(current));
                if (!this.trendBgEnabled) {
                    if (this.trendBgCanvas) {
                        const ctx = this.trendBgCanvas.getContext('2d');
                        ctx.clearRect(0, 0, this.trendBgCanvas.width, this.trendBgCanvas.height);
                    }
                } else {
                    this.drawTrendBg();
                }
            });
        }

        this.initPromise = this.reload();
        } catch (e) {
            alert(`初始化錯誤 (${this.id}):\n${e.message}\n請截圖給 AI 助手。`);
            console.error(e);
        }
    }

    async loadMore() {
        if (!this.oldestTime || this.isLoading) return;
        const start = new Date(this.oldestTime * 1000);
        const days = this.currentPeriod === 'D' ? 365 : 30;
        start.setDate(start.getDate() - days);
        const end = new Date(this.oldestTime * 1000);
        await this.fetchData(start.toISOString().split('T')[0], end.toISOString().split('T')[0], false);
    }

    async reload() {
        if (!this.chart || !this.candleSeries) return;
        this.kbarsCache = [];
        this.pvpMarked = false; // 重設標記，切換合約後重新標註起點
        this.oldestTime = null;
        this.candleSeries.setData([]);
        [5,10,20].forEach(n => this.maSeries[n].setData([]));
        this.macdSeries.line.setData([]);
        this.macdSeries.signal.setData([]);
        this.macdSeries.hist.setData([]);
        
        const today = new Date();
        // 修正：使用本地日期格式 (YYYY-MM-DD)，避免凌晨時抓不到當天數據
        const end = today.toLocaleDateString('en-CA');
        const start = new Date(today.getTime() - 30 * 86400000).toLocaleDateString('en-CA');
        
        await this.fetchData(start, end, true);
    }

    async fetchData(s, e, showLoading = false) {
        this.isLoading = true;
        const overlay = document.getElementById(`${this.id}-loading`);
        if (showLoading) overlay.style.display = 'flex';
        try {
            // 強制抓取 1min 原始數據進行手動聚合 (後端 main.py 回傳的是 list of dicts)
            const res = await fetch(`/api/kbars?symbol=${this.symbol}&start=${s}&end=${e}&period=1min`);
            const data = await res.json();
            
            if (data && data.length > 0) {
                const pMin = (this.currentPeriod === 'D') ? 1440 : parseInt(this.currentPeriod);
                const pSec = pMin * 60;
                
                let aggregated = [];
                let currentBar = null;

                data.forEach(k => {
                    // 數據回歸原始 UTC，不在此位移，位移交給 Formatter
                    const t = Number(k.time);
                    
                    // 關鍵修正：對齊收盤時間標籤 (t - 1)
                    const bucketT = Math.floor((t - 1) / pSec) * pSec;

                    if (!currentBar || bucketT !== currentBar.time) {
                        if (currentBar) aggregated.push(currentBar);
                        currentBar = {
                            time: bucketT,
                            open: k.open,
                            high: k.high,
                            low: k.low,
                            close: k.close,
                            volume: k.volume || 0
                        };
                    } else {
                        currentBar.high = Math.max(currentBar.high, k.high);
                        currentBar.low = Math.min(currentBar.low, k.low);
                        currentBar.close = k.close;
                        currentBar.volume += (k.volume || 0);
                    }
                });
                if (currentBar) aggregated.push(currentBar);

                this.timeOffset = 0;
                this.kbarsCache = aggregated;
                this.kbarsCache.sort((a, b) => a.time - b.time);
                
                if (this.kbarsCache.length > 0) {
                    this.oldestTime = this.kbarsCache[0].time;
                }
                
                this.render();
                
                if (showLoading && this.kbarsCache.length > 0) {
                    this.chart.timeScale().setVisibleLogicalRange({ from: this.kbarsCache.length - 150, to: this.kbarsCache.length - 1 });
                }
            }
        } catch (err) { 
            console.error("Fetch Error:", err); 
        } finally {
            overlay.style.display = 'none';
            this.isLoading = false;
        }
    }

    render() {
        try {
        if (!this.chart || !this.candleSeries) return;
        this.candleSeries.setData(this.kbarsCache);
        
        // 建立時間到索引的快速映射
        this.timeToIndex.clear();
        this.kbarsCache.forEach((k, i) => this.timeToIndex.set(k.time, i));
        
        // 1. 預算並快取均線數據
        if (!this.emaCaches) this.emaCaches = {};
        [5, 10, 20].forEach(n => {
            const emaData = this.calculateEMA(this.kbarsCache, n);
            this.fullMaCaches[n] = emaData;
            this.maSeries[n].setData(emaData);
            if (emaData.length > 0) {
                this.emaCaches[n] = emaData[emaData.length - 1].value;
            }
        });
        
        // 2. 預算並快取 MACD 數據
        const m = this.calculateMACD(this.kbarsCache);
        this.fullMacdCache = m;
        this.macdSeries.line.setData(m.macdLine);
        this.macdSeries.signal.setData(m.signalLine);
        this.macdSeries.hist.setData(m.histogram);

        // 移除 fitContent()，改由使用者自行控制縮放或 scrollToRealTime 控制跟隨
        this.updateMarkers(this.chart.timeScale().getVisibleLogicalRange());
        if (this.pvpEnabled) {
            try { this.drawPVP(); } catch(e) { console.log("PVP Draw Error:", e); }
        }

        // 渲染成交量量柱
        if (this.volumeSeries) {
            const volData = this.kbarsCache.map(k => ({
                time: k.time,
                value: k.volume || 0,
                color: k.close >= k.open ? 'rgba(255, 0, 0, 0.4)' : 'rgba(0, 255, 0, 0.4)'
            }));
            this.volumeSeries.setData(volData);
        }

        // 更新趨勢指示器
        this.updateTrend();
        // 渲染趨勢背景
        this.drawTrendBg();
        } catch (e) {
            alert(`繪圖錯誤 (${this.id}):\n${e.message}\n${e.stack}`);
            console.error(e);
        }
    }

    updateLegend(c, m5, m10, m20, countdownStr = '') {
        const el = document.getElementById(`${this.id}-legend`);
        if (!el) return;
        if (!c) {
            el.innerHTML = `<span style="color: #aaa">${this.currentPeriod} |</span> <span style="color: #666">--</span>`;
            return;
        }
        const color = c.close >= c.open ? '#FF0000' : '#00FF00';
        let text = `
            <span style="color: #aaa">${this.currentPeriod} |</span>
            開 <span style="color: ${color}">${c.open}</span>
            高 <span style="color: ${color}">${c.high}</span>
            低 <span style="color: ${color}">${c.low}</span>
            收 <span style="color: ${color}">${c.close}</span>
        `;
        
        const formatEMA = (val, label, color) => {
            if (typeof val === 'number' && !isNaN(val)) {
                return ` <span style="color:${color}">${label}:${val.toFixed(0)}</span>`;
            }
            return '';
        };

        text += formatEMA(m5, '5EMA', '#FFFF00');
        text += formatEMA(m10, '10EMA', '#00FFFF');
        text += formatEMA(m20, '20EMA', '#B200FF');
        
        el.innerHTML = text + countdownStr;
    }

    showLatestLegend() {
        // 如果滑鼠正在上面，或者正在接收外部連動，則不自動重置為最新價
        if (this.kbarsCache.length === 0 || this.isCrosshairActive || this.isSyncedFocus) return;
        
        const el = document.getElementById(`${this.id}-legend`);
        const last = this.kbarsCache[this.kbarsCache.length - 1];
        
        // 直接從快取拿數據，不再重新計算 2000 根數據，大幅提升效能
        if (!this.emaCaches) this.emaCaches = {};
        const m5 = this.emaCaches[5];
        const m10 = this.emaCaches[10];
        const m20 = this.emaCaches[20];
        

        // 1. 計算倒數計時 (用於 Y 軸價格線標籤)
        let timerLabel = '';
        if (this.currentPeriod !== 'D') {
            const now = Math.floor(Date.now() / 1000);
            let pSec = 60;
            if (this.currentPeriod === '5min') pSec = 300;
            else if (this.currentPeriod === '15min') pSec = 900;
            else if (this.currentPeriod === '30min') pSec = 1800;
            else if (this.currentPeriod === '60min') pSec = 3600;
            
            const remain = pSec - (now % pSec);
            const mm = Math.floor(remain / 60).toString().padStart(2, '0');
            const ss = (remain % 60).toString().padStart(2, '0');
            timerLabel = `${mm}:${ss}`;
        }

        // 2. 更新 Y 軸上的「獨家最新價」標籤
        if (this.candleSeries) {
            this.candleSeries.applyOptions({
                lastValueVisible: false,
                priceFormat: { type: 'price', precision: 0, minMove: 1 }
            });

            const color = last.close >= last.open ? '#FF0000' : '#00FF00';
            if (!this.lastPriceLine) {
                this.lastPriceLine = this.candleSeries.createPriceLine({
                    price: last.close, color: color, lineWidth: 1, lineStyle: 0,
                    axisLabelVisible: true, title: ` ${timerLabel}`,
                });
            } else {
                this.lastPriceLine.applyOptions({ price: last.close, color: color, title: ` ${timerLabel}` });
            }
        }
        
        this.updateLegend(last, m5, m10, m20, ""); // 保持左側乾淨
    }

    calculateEMA(data, count) {
        const res = [];
        const k = 2 / (count + 1);
        let ema = null;
        for (let i = 0; i < data.length; i++) {
            const val = data[i].close || data[i].value;
            ema = (ema === null) ? val : val * k + ema * (1 - k);
            res.push({ time: data[i].time, value: ema });
        }
        return res;
    }

    calculateMACD(data) {
        const ema12 = this.calculateEMA(data, 12);
        const ema26 = this.calculateEMA(data, 26);
        const macdLine = ema12.map((d, i) => ({ time: d.time, value: d.value - ema26[i].value }));
        const signalLine = this.calculateEMA(macdLine, 9);
        const histogram = [];
        for (let i = 0; i < macdLine.length; i++) {
            const sigVal = (signalLine[i] && signalLine[i].value !== undefined) ? signalLine[i].value : 0;
            const val = macdLine[i].value - sigVal;
            const prevVal = i > 0 ? histogram[i - 1].value : 0;
            let color = val >= 0 ? (val >= prevVal ? '#FF0000' : '#800000') : (val <= prevVal ? '#00FF00' : '#008000');
            histogram.push({ time: macdLine[i].time, value: val, color });
        }

        // 對稱正規化縮放到 -30 ~ +30 (中心為 0)
        let maxAbs = 0;
        macdLine.forEach(d => { if (Math.abs(d.value) > maxAbs) maxAbs = Math.abs(d.value); });
        signalLine.forEach(d => { if (Math.abs(d.value) > maxAbs) maxAbs = Math.abs(d.value); });
        histogram.forEach(d => { if (Math.abs(d.value) > maxAbs) maxAbs = Math.abs(d.value); });

        // 乘上一個 1.05 係數，確保不會頂天立地，留有一點上下邊距
        const factor = maxAbs > 0 ? (30 / (maxAbs * 1.05)) : 1;

        const normMacdLine = macdLine.map(d => ({ time: d.time, value: d.value * factor }));
        const normSignalLine = signalLine.map(d => ({ time: d.time, value: d.value * factor }));
        const normHistogram = histogram.map(d => ({ time: d.time, value: d.value * factor, color: d.color }));

        return { macdLine: normMacdLine, signalLine: normSignalLine, histogram: normHistogram };
    }

    updateMarkers(range) {
        try {
            // 如果是多圖模式，隱藏標籤並退出 (避免畫面太擠)
            if (viewMode > 1) {
                if (this.highOverlay) this.highOverlay.style.display = 'none';
                if (this.lowOverlay) this.lowOverlay.style.display = 'none';
                return;
            }

            if (!range || !this.kbarsCache || this.kbarsCache.length === 0 || !this.chart) return;
            
            let f = Math.max(0, Math.floor(range.from)), t = Math.min(this.kbarsCache.length - 1, Math.ceil(range.to));
            if (f > t) return;
            
            let maxK = this.kbarsCache[f], minK = this.kbarsCache[f], maxIdx = f, minIdx = f;
            for (let i = f; i <= t; i++) {
                if (this.kbarsCache[i].high > maxK.high) { maxK = this.kbarsCache[i]; maxIdx = i; }
                if (this.kbarsCache[i].low < minK.low) { minK = this.kbarsCache[i]; minIdx = i; }
            }

            const setPos = (el, k, idx, price, offset) => {
                if (!k || !el) return;
                
                const mainEl = document.getElementById(`${this.id}-main`);
                if (!mainEl) return;
                const containerWidth = mainEl.clientWidth;

                // 使用 timeToCoordinate
                let x = this.chart.timeScale().timeToCoordinate(k.time);
                
                // 如果 timeToCoordinate 不在合理範圍，則回退到幾何換算
                if (x === null || x < 0 || x > containerWidth) {
                    const rangeWidth = range.to - range.from;
                    if (rangeWidth > 0) {
                        const ratio = (idx - range.from) / rangeWidth;
                        // 扣除價格軸寬度 (通常約 60px)
                        x = ratio * (containerWidth - 60); 
                    }
                }

                const y = this.candleSeries.priceToCoordinate(price);

                if (x !== null && y !== null && x >= 0 && x <= containerWidth) {
                    el.innerText = price;
                    el.style.display = 'block';
                    const labelWidth = el.offsetWidth || 50;
                    el.style.left = (x - labelWidth / 2) + 'px';
                    el.style.top = (y + offset) + 'px';
                } else {
                    el.style.display = 'none';
                }
            };

            setPos(this.highOverlay, maxK, maxIdx, maxK.high, -25);
            setPos(this.lowOverlay, minK, minIdx, minK.low, 5);
        } catch (err) {
            console.warn("Markers update failed:", err);
        }
    }

    onTick(price, time) {
        try {
            if (!this.chart || !this.candleSeries || !this.kbarsCache) return;
            
            // 數據回歸原始 UTC，位移交給 Formatter
            let t = (time || Math.floor(Date.now() / 1000));
            const now = Math.floor(Date.now() / 1000);
            
            // 根據目前週期計算秒數 (支援 '1', '5', '15', '30', '60', 'D')
            let pSec = 60;
            if (this.currentPeriod === 'D') {
                pSec = 86400;
            } else {
                pSec = parseInt(this.currentPeriod) * 60 || 60;
            }
            
            const curT = Math.floor(t / pSec) * pSec;
            
            // 如果目前歷史 K 線為空 (例如夜盤維護期間)，直接拿第一筆報價作為開路先鋒！
            if (this.kbarsCache.length === 0) {
                const firstBar = { time: curT, open: price, high: price, low: price, close: price, volume: 1 };
                this.kbarsCache.push(firstBar);
                this.render();
                if (this.pvpEnabled) this.drawPVP();
                if (this.chart) this.chart.timeScale().scrollToRealTime();
                return;
            }
            
            const last = this.kbarsCache[this.kbarsCache.length - 1];
            
            // 強制同步：如果目前價格跳動，不論時間戳如何，都至少要更新最後一根 K 棒
            if (last) {
                if (curT > last.time) {
                    // 進入下一根新 K 棒 (恢復使用真實第一筆成交價作為開盤價)
                    const newBar = { 
                        time: curT, 
                        open: price, 
                        high: price, 
                        low: price, 
                        close: price, 
                        volume: 1 
                    };
                    this.kbarsCache.push(newBar);
                    if (this.kbarsCache.length > 2000) this.kbarsCache.shift();
                    this.render();
                    if (this.pvpEnabled) this.drawPVP();
                    if (this.chart) this.chart.timeScale().scrollToRealTime();
                } else {
                    // 更新目前的最後一根 K 棒 (即使 curT <= last.time 也強制更新，解決時區偏移問題)
                    last.close = price;
                    last.high = Math.max(last.high, price);
                    last.low = Math.min(last.low, price);
                    this.candleSeries.update(last);
                    
                    // 同步更新指標最後一點 (優化：同時更新快取)
                    const lastIdx = this.kbarsCache.length - 1;
                    [5, 10, 20].forEach(n => {
                        if (this.maSeries && this.maSeries[n]) {
                            const emaArr = this.calculateEMA(this.kbarsCache, n);
                            if (emaArr.length > 0) {
                                const lastPoint = emaArr[emaArr.length - 1];
                                this.fullMaCaches[n][lastIdx] = lastPoint;
                                this.maSeries[n].update(lastPoint);
                                this.emaCaches[n] = lastPoint.value;
                            }
                        }
                    });

                    // 新增：同步更新成交量量柱
                    this.volumeSeries.update({
                        time: last.time,
                        value: last.volume || 0,
                        color: last.close >= last.open ? 'rgba(255, 0, 0, 0.4)' : 'rgba(0, 255, 0, 0.4)'
                    });

                    if (this.macdSeries && this.macdSeries.line) {
                        const m = this.calculateMACD(this.kbarsCache);
                        if (m.macdLine.length > 0) {
                            const lastMACD = m.macdLine[m.macdLine.length - 1];
                            const lastSignal = m.signalLine[m.signalLine.length - 1];
                            const lastHist = m.histogram[m.histogram.length - 1];
                            
                            this.fullMacdCache.macdLine[lastIdx] = lastMACD;
                            this.fullMacdCache.signalLine[lastIdx] = lastSignal;
                            this.fullMacdCache.histogram[lastIdx] = lastHist;
                            
                            this.macdSeries.line.update(lastMACD);
                            this.macdSeries.signal.update(lastSignal);
                            this.macdSeries.hist.update(lastHist);
                        }
                    }
                }
            }
            // 關鍵修正：只有在既沒滑鼠指著，也沒有在連動同步時，才自動更新數據標籤為最新價
            if (!this.isCrosshairActive && !this.isSyncedFocus) {
                this.showLatestLegend();
            }

            // 更新趨勢指示器
            this.updateTrend();
            // 更新趨勢背景
            this.drawTrendBg();
        } catch (err) {
            console.debug("Tick sync suppressed error:", err);
        }
    }

    updateTrend() {
        try {
        if (!this.trendIndicator) return;

        const trendBgWrap = document.getElementById(`${this.id}-trend-bg-wrap`);

        // 僅在 60K 顯示，其餘隱藏
        if (this.currentPeriod !== '60min') {
            this.trendIndicator.style.display = 'none';
            if (trendBgWrap) trendBgWrap.style.display = 'none';
            return;
        }

        if (trendBgWrap) trendBgWrap.style.display = 'flex';
        this.trendIndicator.style.display = 'flex';

        const lastBar = this.kbarsCache[this.kbarsCache.length - 1];
        const ema20Arr = this.fullMaCaches[20];
        const difArr = this.fullMacdCache.macdLine;
        const macdArr = this.fullMacdCache.signalLine;

        if (!ema20Arr || !difArr || !macdArr || 
            ema20Arr.length === 0 || difArr.length === 0 || macdArr.length === 0) return;

        const lastEma20 = ema20Arr[ema20Arr.length - 1].value;
        const lastDif = difArr[difArr.length - 1].value;
        const lastMacd = macdArr[macdArr.length - 1].value;
        const close = lastBar.close;

        let statusText = '中性';
        let color = '#f1c40f'; // Yellow

        // 多方：價格 > 20MA 且 DIF > MACD
        if (close > lastEma20 && lastDif > lastMacd) {
            statusText = '多方';
            color = '#ff4444'; // Red
        } 
        // 空方：價格 < 20MA 且 DIF < MACD
        else if (close < lastEma20 && lastDif < lastMacd) {
            statusText = '空方';
            color = '#44ff44'; // Green
        }

        const dot = this.trendIndicator.querySelector('.trend-dot');
        const txt = this.trendIndicator.querySelector('.trend-text');
        
        if (dot) {
            dot.style.backgroundColor = color;
            dot.style.boxShadow = `0 0 10px ${color}`;
        }
        if (txt) {
            txt.innerText = `方向：${statusText}`;
            txt.style.color = color;
        }
        } catch (e) {
            alert(`趨勢更新錯誤:\n${e.message}\n${e.stack}`);
            console.error(e);
        }
    }

    drawTrendBg() {
        if (!this.trendBgEnabled || this.currentPeriod !== '60min' || !this.trendBgCanvas || !this.kbarsCache.length) {
            if (this.trendBgCanvas) {
                const ctx = this.trendBgCanvas.getContext('2d');
                ctx.clearRect(0, 0, this.trendBgCanvas.width, this.trendBgCanvas.height);
            }
            return;
        }

        const canvas = this.trendBgCanvas;
        const ctx = canvas.getContext('2d');
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        const timeScale = this.chart.timeScale();
        const visibleRange = timeScale.getVisibleLogicalRange();
        if (!visibleRange) return;

        const ema20Arr = this.fullMaCaches[20];
        const difArr = this.fullMacdCache.macdLine;
        const macdArr = this.fullMacdCache.signalLine;

        if (!ema20Arr || !difArr || !macdArr) return;

        // 計算 K 棒寬度
        const p1 = timeScale.timeToCoordinate(this.kbarsCache[0].time);
        const p2 = this.kbarsCache.length > 1 ? timeScale.timeToCoordinate(this.kbarsCache[1].time) : null;
        const barWidth = p2 !== null ? Math.abs(p2 - p1) : 10;

        this.kbarsCache.forEach((k, idx) => {
            const x = timeScale.timeToCoordinate(k.time);
            // 效能優化：只畫可見範圍內的
            if (x === null || x < -barWidth || x > canvas.width + barWidth) return;

            const ema20 = ema20Arr[idx] ? ema20Arr[idx].value : null;
            const dif = difArr[idx] ? difArr[idx].value : null;
            const macd = macdArr[idx] ? macdArr[idx].value : null;

            if (ema20 === null || dif === null || macd === null) return;

            let color = null;
            if (k.close > ema20 && dif > macd) {
                color = 'rgba(255, 0, 0, 0.12)'; // 多方：淡紅
            } else if (k.close < ema20 && dif < macd) {
                color = 'rgba(0, 255, 0, 0.12)'; // 空方：淡綠
            } else {
                color = 'rgba(255, 255, 0, 0.05)'; // 中性：極淡黃
            }

            if (color) {
                ctx.fillStyle = color;
                ctx.fillRect(x - barWidth / 2, 0, barWidth, canvas.height);
            }
        });
    }

    syncFromExternal(time) {
        // 最後防線：檢查全域開關
        const syncEl = document.getElementById('sync-toggle-btn');
        if (!syncEl?.checked) {
            this.isSyncedFocus = false;
            return;
        }

        // 如果本機正有實體滑鼠指著，則拒絕外部同步
        if (this.isCrosshairActive || !this.chart || this.isLoading || !this.kbarsCache || this.kbarsCache.length === 0) return;
        
        if (time === null || time === undefined) {
            this.isSyncedFocus = false;
            this.updateLegend(null);
            this.chart.setCrosshairPosition(undefined, undefined, this.candleSeries);
            this.macdChart.setCrosshairPosition(undefined, undefined, this.macdSeries.line);
            return;
        }

        this.isSyncedFocus = true; // 標記為正在連動中
        this.isSyncing = true;
        try {
            const lastK = this.kbarsCache[this.kbarsCache.length - 1];
            
            // 如果同步時間超過最後一根 K 棒，則只移動準星，不顯示數據
            if (time > lastK.time) {
                this.updateLegend(null);
                this.chart.setCrosshairPosition(undefined, time, this.candleSeries);
                this.macdChart.setCrosshairPosition(undefined, time, this.macdSeries.line);
                return;
            }

            let targetK = null;
            // 尋找對應時間的 K 棒
            for (let i = this.kbarsCache.length - 1; i >= 0; i--) {
                if (this.kbarsCache[i].time <= time) {
                    // 只有在時間完全匹配時才顯示數據，否則只顯示準星
                    if (this.kbarsCache[i].time === time) {
                        targetK = this.kbarsCache[i];
                    }
                    break;
                }
            }

            // 更新準星位置（允許在空白處顯示）
            this.chart.setCrosshairPosition(undefined, time, this.candleSeries);
            this.macdChart.setCrosshairPosition(undefined, time, this.macdSeries.line);
            
            if (targetK) {
                // 3. 直接從快取拿數據，零運算
                const idx = this.timeToIndex.get(targetK.time);
                let v5, v10, v20;
                if (idx !== undefined) {
                    v5 = this.fullMaCaches[5][idx] ? this.fullMaCaches[5][idx].value : undefined;
                    v10 = this.fullMaCaches[10][idx] ? this.fullMaCaches[10][idx].value : undefined;
                    v20 = this.fullMaCaches[20][idx] ? this.fullMaCaches[20][idx].value : undefined;
                }
                
                this.updateLegend(targetK, v5, v10, v20, "");
            } else {
                this.updateLegend(null);
            }
        } catch (err) {
            console.error("同步失敗", err);
        } finally {
            this.isSyncing = false;
        }
    }

    clearSync() {
        // 如果本機正有滑鼠指著，則不執行清除同步
        if (this.isCrosshairActive || !this.chart) return;
        this.isSyncedFocus = false; // 解除連動標記
        this.isSyncing = true;
        this.chart.setCrosshairPosition(undefined, undefined, this.candleSeries);
        this.macdChart.setCrosshairPosition(undefined, undefined, this.macdSeries.line);
        this.showLatestLegend();
        this.isSyncing = false;
    }

    calculatePVP() {
        if (!this.kbarsCache || this.kbarsCache.length === 0) return null;
        
        // 1. 利用日盤與夜盤的「時間斷層」尋找起算點
        // 台灣期貨日盤於 13:45 收盤 (小時為 13)，夜盤於 15:00 開盤 (小時為 14 或 15，視對齊而定)
        // 只要找到「前一根在 13 點以前，這一根在 14 到 16 點之間」，就是絕對精準的夜盤開盤第一根！
        let sessionStartTime = 0;
        for (let i = this.kbarsCache.length - 1; i > 0; i--) {
            const currentHour = new Date(this.kbarsCache[i].time * 1000).getHours();
            const prevHour = new Date(this.kbarsCache[i-1].time * 1000).getHours();
            
            if (currentHour >= 14 && currentHour <= 16 && prevHour <= 13) {
                sessionStartTime = this.kbarsCache[i].time;
                break;
            }
        }

        // 如果跨週末或斷線找不到轉折點，只好拿畫面上的第一根當作備案
        if (sessionStartTime === 0) sessionStartTime = this.kbarsCache[0].time;

        const targetBars = this.kbarsCache.filter(k => k.time >= sessionStartTime);
        if (targetBars.length === 0) return null;

        // 除錯：標記起點 (移除 Alert 避免阻塞)
        if (!this.pvpMarked && this.candleSeries) {
            this.pvpMarked = true;
            try {
                this.candleSeries.setMarkers([
                    { time: sessionStartTime, position: 'belowBar', color: '#2196F3', shape: 'arrowUp', text: 'PVP START' }
                ]);
            } catch(e) {}
        }
        
        console.log(`[PVP] Calculating for ${targetBars.length} bars from session starting at ${new Date(sessionStartTime*1000).toLocaleString()}`);

        const high = Math.max(...targetBars.map(k => k.high));
        const low = Math.min(...targetBars.map(k => k.low));
        const range = high - low;
        if (range <= 0) return null;
        
        const rowCount = 100; // 提升至 100 階，匹配專業精度
        const step = range / rowCount;
        const buckets = Array.from({ length: rowCount }, (_, i) => ({
            low: low + i * step,
            high: low + (i + 1) * step,
            upVol: 0,
            downVol: 0,
            totalVol: 0
        }));

        targetBars.forEach(k => {
            const barVol = (k.volume || 1);
            const isUp = k.close >= k.open;
            const bStart = Math.max(0, Math.floor((k.low - low) / step));
            const bEnd = Math.min(rowCount - 1, Math.floor((k.high - low) / step));
            const involvedBuckets = bEnd - bStart + 1;
            const volPerBucket = barVol / involvedBuckets;

            for (let i = bStart; i <= bEnd; i++) {
                if (isUp) buckets[i].upVol += volPerBucket;
                else buckets[i].downVol += volPerBucket;
                buckets[i].totalVol += volPerBucket;
            }
        });

        // 2. 尋找 POC (成交量最大的格子)
        let pocIdx = 0;
        let maxV = -1;
        buckets.forEach((b, i) => { 
            if (b.totalVol > maxV) { maxV = b.totalVol; pocIdx = i; } 
        });
        const pocPrice = (buckets[pocIdx].low + buckets[pocIdx].high) / 2;

        // 3. 採用 TradingView 標準備 2-Row 擴張演算法計算 VA (70%)
        const totalVol = targetBars.reduce((sum, k) => sum + (k.volume || 1), 0);
        const targetVA = totalVol * 0.7;
        let vaVol = buckets[pocIdx].totalVol;
        let upIdx = pocIdx, dnIdx = pocIdx;
        
        while (vaVol < targetVA && (upIdx < rowCount - 1 || dnIdx > 0)) {
            // 比較上方兩格與下方兩格的總量
            const up2 = (upIdx < rowCount - 1 ? buckets[upIdx + 1].totalVol : 0) + 
                        (upIdx < rowCount - 2 ? buckets[upIdx + 2].totalVol : 0);
            const dn2 = (dnIdx > 0 ? buckets[dnIdx - 1].totalVol : 0) + 
                        (dnIdx > 1 ? buckets[dnIdx - 2].totalVol : 0);
            
            if (up2 >= dn2 && upIdx < rowCount - 1) {
                upIdx++;
                vaVol += buckets[upIdx].totalVol;
            } else if (dnIdx > 0) {
                dnIdx--;
                vaVol += buckets[dnIdx].totalVol;
            } else {
                break;
            }
        }

        return {
            buckets,
            poc: pocPrice,
            vah: buckets[upIdx].high,
            val: buckets[dnIdx].low,
            maxVol: Math.max(...buckets.map(b => b.totalVol))
        };
    }

    drawPVP() {
        if (!this.chart || !this.candleSeries || !this.pvpCanvas || !this.pvpEnabled) return;
        const mainEl = document.getElementById(`${this.id}-main`);
        if (!mainEl) return;

        // Chrome 補強：如果畫布尺寸不對，強制重新設定
        if (this.pvpCanvas.width !== mainEl.clientWidth || this.pvpCanvas.height !== mainEl.clientHeight) {
            this.pvpCanvas.width = mainEl.clientWidth;
            this.pvpCanvas.height = mainEl.clientHeight;
        }

        const ctx = this.pvpCanvas.getContext('2d');
        const data = this.calculatePVP();
        ctx.clearRect(0, 0, this.pvpCanvas.width, this.pvpCanvas.height);
        
        if (!data) return;

        const canvasWidth = this.pvpCanvas.width;
        const maxWidth = canvasWidth * 0.35; // 稍微增加寬度至 35%

        data.buckets.forEach(b => {
            const yHigh = this.candleSeries.priceToCoordinate(b.high);
            const yLow = this.candleSeries.priceToCoordinate(b.low);
            if (yHigh === null || yLow === null) return;

            const h = Math.abs(yLow - yHigh);
            const upW = (b.upVol / data.maxVol) * maxWidth;
            const dnW = (b.downVol / data.maxVol) * maxWidth;

            // 畫買方量 (Cyan)
            ctx.fillStyle = 'rgba(0, 255, 255, 0.4)';
            ctx.fillRect(0, yHigh, upW, h - 1);
            
            // 畫賣方量 (Pink)
            ctx.fillStyle = 'rgba(255, 0, 255, 0.4)';
            ctx.fillRect(upW, yHigh, dnW, h - 1);
        });

        // 更新 POC/VAH/VAL 線條
        this.updatePvpLine('poc', data.poc, '#FFFFFF', 'POC');
        this.updatePvpLine('vah', data.vah, '#FF0000', 'VAH');
        this.updatePvpLine('val', data.val, '#00FF00', 'VAL');
    }

    updatePvpLine(id, price, color, title) {
        if (this.pvpLines[id]) {
            this.candleSeries.removePriceLine(this.pvpLines[id]);
        }
        this.pvpLines[id] = this.candleSeries.createPriceLine({
            price: price,
            color: color,
            lineWidth: 2,
            lineStyle: 0, // 實線
            axisLabelVisible: true,
            title: title
        });
    }

    resize() {
        const containerId = this.idAttr || this.id;
        const pane = document.getElementById(containerId);
        const mainEl = document.getElementById(`${this.id}-main`);
        const macdEl = document.getElementById(`${this.id}-macd-chart`);
        
        if (pane && pane.clientWidth > 0 && this.chart) {
            const w = pane.clientWidth;
            const h = mainEl.clientHeight;
            this.chart.resize(w, h);
            this.macdChart.resize(w, macdEl.clientHeight);
            
            if (this.pvpCanvas) {
                this.pvpCanvas.width = w;
                this.pvpCanvas.height = h;
                this.drawPVP();
            }

            setTimeout(() => {
                const range = this.chart.timeScale().getVisibleLogicalRange();
                if (range) this.updateMarkers(range);
            }, 200);
        }
    }
}

async function loadInstitutionalRankings() {
    const loadingView = document.getElementById('stock-loading-view');
    const tablesView = document.getElementById('stock-tables-view');
    const dateLabel = document.getElementById('stock-rank-date');
    const buyBody = document.getElementById('stock-buy-rank-body');
    const sellBody = document.getElementById('stock-sell-rank-body');
    
    if (!loadingView || !tablesView) return;
    
    // 顯示載入中
    loadingView.style.display = 'flex';
    tablesView.style.display = 'none';
    dateLabel.innerText = '📅 載入中...';
    
    try {
        const res = await fetch('/api/institutional_rankings');
        const data = await res.json();
        
        if (data.status === 'success') {
            dateLabel.innerText = `📅 資料日期：${data.date}`;
            
            // 填充買超表格
            buyBody.innerHTML = data.buy_rank.map((item, idx) => {
                const totalColor = item.total >= 0 ? '#ff4444' : '#44ff44';
                const totalPrefix = item.total > 0 ? '+' : '';
                return `
                    <tr>
                        <td style="text-align: center; font-weight: bold; color: #ff9f43; padding: 8px 5px;">${idx + 1}</td>
                        <td style="padding: 8px 5px;"><span class="stock-code-btn" style="color: #4facfe; cursor: pointer; text-decoration: underline; font-family: monospace; font-weight: bold;">${item.code}</span></td>
                        <td style="font-weight: 500; color: #eee; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100px; padding: 8px 5px;">${item.name}</td>
                        <td style="text-align: right; color: ${item.foreign >= 0 ? '#ff4444' : '#44ff44'}; padding: 8px 5px;">${item.foreign > 0 ? '+' : ''}${item.foreign.toLocaleString()}</td>
                        <td style="text-align: right; color: ${item.it >= 0 ? '#ff4444' : '#44ff44'}; padding: 8px 5px;">${item.it > 0 ? '+' : ''}${item.it.toLocaleString()}</td>
                        <td style="text-align: right; color: ${item.dealer >= 0 ? '#ff4444' : '#44ff44'}; padding: 8px 5px;">${item.dealer > 0 ? '+' : ''}${item.dealer.toLocaleString()}</td>
                        <td style="text-align: right; font-weight: bold; color: ${totalColor}; padding: 8px 5px;">${totalPrefix}${item.total.toLocaleString()}</td>
                    </tr>
                `;
            }).join('');
            
            // 填充賣超表格
            sellBody.innerHTML = data.sell_rank.map((item, idx) => {
                const totalColor = item.total >= 0 ? '#ff4444' : '#44ff44';
                const totalPrefix = item.total > 0 ? '+' : '';
                return `
                    <tr>
                        <td style="text-align: center; font-weight: bold; color: #a55eea; padding: 8px 5px;">${idx + 1}</td>
                        <td style="padding: 8px 5px;"><span class="stock-code-btn" style="color: #4facfe; cursor: pointer; text-decoration: underline; font-family: monospace; font-weight: bold;">${item.code}</span></td>
                        <td style="font-weight: 500; color: #eee; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100px; padding: 8px 5px;">${item.name}</td>
                        <td style="text-align: right; color: ${item.foreign >= 0 ? '#ff4444' : '#44ff44'}; padding: 8px 5px;">${item.foreign > 0 ? '+' : ''}${item.foreign.toLocaleString()}</td>
                        <td style="text-align: right; color: ${item.it >= 0 ? '#ff4444' : '#44ff44'}; padding: 8px 5px;">${item.it > 0 ? '+' : ''}${item.it.toLocaleString()}</td>
                        <td style="text-align: right; color: ${item.dealer >= 0 ? '#ff4444' : '#44ff44'}; padding: 8px 5px;">${item.dealer > 0 ? '+' : ''}${item.dealer.toLocaleString()}</td>
                        <td style="text-align: right; font-weight: bold; color: ${totalColor}; padding: 8px 5px;">${totalPrefix}${item.total.toLocaleString()}</td>
                    </tr>
                `;
            }).join('');
            
            // 隱藏載入中，顯示表格
            loadingView.style.display = 'none';
            tablesView.style.display = 'grid';
        } else {
            dateLabel.innerText = '❌ 載入失敗';
            loadingView.innerHTML = `<div style="color: #ff4444; font-size: 1.1rem; text-align: center; max-width: 400px; line-height: 1.5;"><i class="fas fa-exclamation-triangle" style="font-size: 2rem; margin-bottom: 10px;"></i><br>同步資料失敗，請確認伺服器連線或網路狀態。</div>`;
        }
    } catch (e) {
        console.error("載入三大法人排行失敗", e);
        dateLabel.innerText = '❌ 載入失敗';
        loadingView.innerHTML = `<div style="color: #ff4444; font-size: 1.1rem; text-align: center; max-width: 400px; line-height: 1.5;"><i class="fas fa-exclamation-triangle" style="font-size: 2rem; margin-bottom: 10px;"></i><br>同步資料發生異常：<br>${e.message}</div>`;
    }
}

async function runScreener() {
    const biasEl = document.getElementById('screener-bias');
    const ratioEl = document.getElementById('screener-inst-ratio');
    const sentimentEl = document.getElementById('screener-sentiment');
    const resultsBody = document.getElementById('screener-results-body');
    const runBtn = document.getElementById('run-screener-btn');
    
    if (!resultsBody) return;
    
    resultsBody.innerHTML = `
        <tr>
            <td colspan="9" style="text-align: center; color: #aaa; padding: 50px;">
                <div class="loading-spinner" style="border: 3px solid rgba(255, 159, 67, 0.1); border-top: 3px solid #ff9f43; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; margin: 0 auto 10px auto;"></div>
                <div style="font-size: 0.9rem; letter-spacing: 0.5px; color: #ff9f43;">正在分析台股主力突破標的，請稍候...</div>
            </td>
        </tr>
    `;
    
    if (runBtn) {
        runBtn.disabled = true;
        runBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> 正在篩選...`;
    }
    
    try {
        const turnoverEl = document.getElementById('screener-turnover-min');
        const payload = {
            turnover_min:    parseInt(turnoverEl ? turnoverEl.value : 30000000),
            max_decline_pct: -3.5
        };
        
        const res = await fetch('/api/screener/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        
        const data = await res.json();
        
        if (data.status === 'success' && Array.isArray(data.data)) {
            const list = data.data;
            if (list.length === 0) {
                resultsBody.innerHTML = `
                    <tr>
                        <td colspan="9" style="text-align: center; color: #888; padding: 50px;">
                            <i class="fas fa-info-circle" style="font-size: 1.5rem; color: #555; margin-bottom: 10px; display: block;"></i>
                            查無符合目前條件之主力突破股，建議放寬篩選標準（如調大最大乖離或調低法人佔比）。
                        </td>
                    </tr>
                `;
            } else {
                resultsBody.innerHTML = list.map(item => {
                    const biasColor = item.bias >= 0 ? '#ff4444' : '#44ff44';
                    const gain20Color = item.gain_20 >= 0 ? '#ff4444' : '#44ff44';
                    const gain60Color = item.gain_60 >= 0 ? '#ff4444' : '#44ff44';
                    
                    // 方案 B：高品質階級標籤
                    let tierBadge = '';
                    if (item.tier_level === 1) {
                        tierBadge = `<span style="background: linear-gradient(135deg, rgba(255, 215, 0, 0.2), rgba(255, 165, 0, 0.2)); color: #ffd700; border: 1px solid rgba(255, 215, 0, 0.4); padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; box-shadow: 0 0 8px rgba(255, 215, 0, 0.2); white-space: nowrap;">👑 黃金滿貫</span>`;
                    } else if (item.tier_level === 2) {
                        tierBadge = `<span style="background: linear-gradient(135deg, rgba(165, 94, 234, 0.2), rgba(255, 0, 128, 0.2)); color: #e056fd; border: 1px solid rgba(165, 94, 234, 0.4); padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; white-space: nowrap;">🥈 強勢雙雄</span>`;
                    } else if (item.tier_level === 3) {
                        tierBadge = `<span style="background: rgba(255, 159, 67, 0.15); color: #ff9f43; border: 1px solid rgba(255, 159, 67, 0.3); padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; white-space: nowrap;">🥉 投信鎖碼</span>`;
                    } else if (item.tier_level === 4) {
                        tierBadge = `<span style="background: rgba(79, 172, 254, 0.15); color: #4facfe; border: 1px solid rgba(79, 172, 254, 0.3); padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; white-space: nowrap;">🏅 外資鎖碼</span>`;
                    } else {
                        tierBadge = `<span style="background: rgba(120, 120, 120, 0.15); color: #aaa; border: 1px solid rgba(120, 120, 120, 0.3); padding: 3px 8px; border-radius: 4px; font-size: 0.75rem; font-weight: bold; white-space: nowrap;">主力佈局</span>`;
                    }
                    
                    let badges = '';
                    if (item.sync_buy) {
                        badges += `<span style="background: rgba(255, 68, 68, 0.15); color: #ff4444; border: 1px solid rgba(255, 68, 68, 0.3); padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; margin-right: 4px; display: inline-block;">🔥 三人同買</span>`;
                    }
                    if (item.investment_strike > 0) {
                        badges += `<span style="background: rgba(255, 159, 67, 0.15); color: #ff9f43; border: 1px solid rgba(255, 159, 67, 0.3); padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; margin-right: 4px; display: inline-block;">投信連買 ${item.investment_strike}D</span>`;
                    }
                    if (item.foreign_strike > 0) {
                        badges += `<span style="background: rgba(79, 172, 254, 0.15); color: #4facfe; border: 1px solid rgba(79, 172, 254, 0.3); padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; margin-right: 4px; display: inline-block;">外資連買 ${item.foreign_strike}D</span>`;
                    }
                    if (item.mention_count > 0) {
                        badges += `<span style="background: rgba(165, 94, 234, 0.15); color: #a55eea; border: 1px solid rgba(165, 94, 234, 0.3); padding: 2px 6px; border-radius: 4px; font-size: 0.7rem; font-weight: bold; display: inline-block;">💬 輿情 ${item.mention_count}</span>`;
                    }
                    if (!badges) {
                        badges = `<span style="color: #666; font-size: 0.75rem;">主力溫和佈局</span>`;
                    }
                    
                    return `
                        <tr>
                            <td style="padding: 8px 4px;">
                                <span class="stock-code-btn" style="color: #4facfe; cursor: pointer; text-decoration: underline; font-family: monospace; font-weight: bold; font-size: 0.85rem;">${item.code}</span>
                            </td>
                            <td style="padding: 8px 4px; color: #fff; font-size: 0.8rem; font-weight: bold;">${item.name}</td>
                            <td style="padding: 8px 4px; text-align: center;">${tierBadge}</td>
                            <td style="padding: 8px 4px; text-align: right; font-weight: 600; color: #fff;">${item.close.toFixed(2)}</td>
                            <td style="padding: 8px 4px; text-align: right; color: ${biasColor}; font-weight: 500;">${item.bias > 0 ? '+' : ''}${item.bias}%</td>
                            <td style="padding: 8px 4px; text-align: right; color: ${gain20Color}; font-weight: 500;">${item.gain_20 > 0 ? '+' : ''}${item.gain_20}%</td>
                            <td style="padding: 8px 4px; text-align: right; color: ${gain60Color}; font-weight: 500;">${item.gain_60 > 0 ? '+' : ''}${item.gain_60}%</td>
                            <td style="padding: 8px 4px; text-align: right; color: #ff9f43; font-weight: 600;">${item.inst_ratio_5d}%</td>
                            <td style="padding: 8px 4px; text-align: center;">${badges}</td>
                        </tr>
                    `;
                }).join('');
            }
        } else {
            resultsBody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: #ff4444; padding: 40px;">❌ 篩選失敗，請稍後再試。</td></tr>`;
        }
    } catch (e) {
        console.error("執行選股篩選失敗:", e);
        resultsBody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: #ff4444; padding: 40px;">❌ 選股計算發生異常：<br>${e.message}</td></tr>`;
    } finally {
        if (runBtn) {
            runBtn.disabled = false;
            runBtn.innerHTML = `🔍 執行策略篩選`;
        }
    }
}

async function startApp(contractCode) {
    const ENABLE_INITIAL_LOADING = true; // 🎯 改為 false 即可一秒關閉/不建此 Loading 遮罩
    
    const initOverlay = document.getElementById('initial-loading-overlay');
    if (ENABLE_INITIAL_LOADING && initOverlay) {
        initOverlay.style.display = 'flex';
        initOverlay.style.opacity = '1';
        const loadText = document.getElementById('initial-loading-text');
        if (loadText) loadText.innerText = "正在登入並同步永豐金證券伺服器...";
    } else {
        if (initOverlay) initOverlay.style.display = 'none';
    }
    
    document.getElementById('login-container').style.display = 'none';
    document.getElementById('app-container').style.display = 'flex';
    
    // 關鍵輔助函數：根據「期貨/股票」動態生成及更新合約選單內容
    const updateContractSelector = (market, defaultCode = null) => {
        const selector = document.getElementById('contract-selector');
        if (!selector) return;
        
        // 正規化預設值
        let actualDefault = defaultCode;
        if (market === 'stocks') {
            if (!actualDefault || isNaN(actualDefault)) {
                actualDefault = '2330';
            }
        } else {
            if (!actualDefault || !actualDefault.includes('F')) {
                actualDefault = 'TXFR1';
            }
        }
        
        selector.innerHTML = '';
        if (market === 'futures') {
            const opt1 = document.createElement('option');
            opt1.value = 'TXFR1';
            opt1.innerText = '台指近月全 (TXF)';
            const opt2 = document.createElement('option');
            opt2.value = 'MXFR1';
            opt2.innerText = '小台近月全 (MXF)';
            const opt3 = document.createElement('option');
            opt3.value = 'TMFR1';
            opt3.innerText = '微台近月全 (TMF)';
            selector.appendChild(opt1);
            selector.appendChild(opt2);
            selector.appendChild(opt3);
            
            selector.value = actualDefault;
        } else { // stocks
            const opt1 = document.createElement('option');
            opt1.value = '2330';
            opt1.innerText = '台積電 (2330)';
            const opt2 = document.createElement('option');
            opt2.value = '2317';
            opt2.innerText = '鴻海 (2317)';
            const opt3 = document.createElement('option');
            opt3.value = '2454';
            opt3.innerText = '聯發科 (2454)';
            const opt4 = document.createElement('option');
            opt4.value = '2308';
            opt4.innerText = '台達電 (2308)';
            const opt5 = document.createElement('option');
            opt5.value = '2603';
            opt5.innerText = '長榮 (2603)';
            selector.appendChild(opt1);
            selector.appendChild(opt2);
            selector.appendChild(opt3);
            selector.appendChild(opt4);
            selector.appendChild(opt5);
            
            // 如果所選股票代號不在常規推薦中，則動態追加選項，保證下拉選單不會壞掉
            const normalStocks = ['2330', '2317', '2454', '2308', '2603'];
            if (actualDefault && !normalStocks.includes(actualDefault)) {
                const optTemp = document.createElement('option');
                optTemp.value = actualDefault;
                optTemp.innerText = `個股 (${actualDefault})`;
                selector.appendChild(optTemp);
            }
            
            selector.value = actualDefault;
        }
    };

    // 0. 市場選擇器切換 (期貨/股票) 與雙頁籤分頁控制
    const marketSelector = document.getElementById('market-type-selector');
    const appContainer = document.getElementById('app-container');
    const tabsBar = document.getElementById('stock-tabs-bar');
    const placeholder = document.getElementById('stock-container-placeholder');
    const panesContainer = document.getElementById('panes-container');
    
    const tabRankingBtn = document.getElementById('tab-ranking-btn');
    const tabScreenerBtn = document.getElementById('tab-screener-btn');
    
    let activeStockTab = 'ranking'; // 預設股票模式時顯示「法人排行」
    
    const applyMarket = (market) => {
        const rankingsView = document.getElementById('stock-rankings-view');
        const screenerView = document.getElementById('stock-screener-view');
        
        if (market === 'stocks') {
            // 🎯 股票模式：啟用 market-stocks 類別，隱藏期貨雜訊與下方圖表
            appContainer.classList.add('market-stocks');
            
            // 顯示股票選股的主容器與頂部頁籤
            if (tabsBar) tabsBar.style.display = 'flex';
            if (placeholder) placeholder.style.display = 'flex';
            
            // 更新頁籤與視窗的可見度
            if (activeStockTab === 'ranking') {
                if (tabRankingBtn) {
                    tabRankingBtn.style.color = '#ff9f43';
                    tabRankingBtn.style.borderBottomColor = '#ff9f43';
                }
                if (tabScreenerBtn) {
                    tabScreenerBtn.style.color = '#888';
                    tabScreenerBtn.style.borderBottomColor = 'transparent';
                }
                if (rankingsView) rankingsView.style.display = 'flex';
                if (screenerView) screenerView.style.display = 'none';
            } else { // screener
                if (tabScreenerBtn) {
                    tabScreenerBtn.style.color = '#ff9f43';
                    tabScreenerBtn.style.borderBottomColor = '#ff9f43';
                }
                if (tabRankingBtn) {
                    tabRankingBtn.style.color = '#888';
                    tabRankingBtn.style.borderBottomColor = 'transparent';
                }
                if (screenerView) screenerView.style.display = 'flex';
                if (rankingsView) rankingsView.style.display = 'none';
            }
            
            // 異步加載排行數據
            loadInstitutionalRankings();
        } else { // futures
            appContainer.classList.remove('market-stocks');
            if (tabsBar) tabsBar.style.display = 'none';
            if (placeholder) placeholder.style.display = 'none';
            
            // 強制重設圖表尺寸，避免黑屏或跑版
            setTimeout(() => {
                if (typeof panes !== 'undefined' && Array.isArray(panes)) {
                    panes.forEach(p => { if (p) p.resize(); });
                }
            }, 100);
        }
    };
    
    if (marketSelector) {
        // 從本地暫存讀取上一次的選擇市場，預設為期貨看盤
        const savedMarket = localStorage.getItem('global-market-type') || 'futures';
        
        // 初始載入時，生成對應市場的選單項目
        updateContractSelector(savedMarket, contractCode);
        
        marketSelector.value = savedMarket;
        applyMarket(savedMarket);
        
        marketSelector.addEventListener('change', (e) => {
            const market = e.target.value;
            localStorage.setItem('global-market-type', market);
            
            // 當切換市場時，自動載入該市場之預設合約
            const defaultCode = (market === 'futures') ? 'TXFR1' : '2330';
            updateContractSelector(market, defaultCode);
            
            // 🎯 只有當切換回期貨市場時，才觸發合約變動與後端訂閱！股票模式下我們不訂閱任何股票
            if (market === 'futures') {
                const selector = document.getElementById('contract-selector');
                if (selector && typeof selector.onchange === 'function') {
                    selector.onchange();
                }
            }
            
            applyMarket(market);
        });
    }
    
    // 綁定頁籤按鈕點擊事件
    if (tabRankingBtn) {
        tabRankingBtn.onclick = () => {
            activeStockTab = 'ranking';
            applyMarket('stocks');
        };
    }
    if (tabScreenerBtn) {
        tabScreenerBtn.onclick = () => {
            activeStockTab = 'screener';
            applyMarket('stocks');
        };
    }
    
    // 0.1 綁定策略過濾調參器與滑桿
    const biasSlider = document.getElementById('screener-bias');
    const biasVal = document.getElementById('screener-bias-val');
    if (biasSlider && biasVal) {
        biasSlider.addEventListener('input', (e) => {
            biasVal.innerText = `${e.target.value}%`;
        });
    }
    
    const instSlider = document.getElementById('screener-inst-ratio');
    const instVal = document.getElementById('screener-inst-ratio-val');
    if (instSlider && instVal) {
        instSlider.addEventListener('input', (e) => {
            instVal.innerText = `${e.target.value}%`;
        });
    }
    
    // 0.2 綁定執行策略篩選與數據同步
    const runScreenerBtn = document.getElementById('run-screener-btn');
    if (runScreenerBtn) {
        runScreenerBtn.onclick = () => {
            runScreener();
        };
    }
    
    const syncScreenerBtn = document.getElementById('sync-screener-btn');
    const loadingView = document.getElementById('stock-loading-view');
    const tablesView = document.getElementById('stock-tables-view');
    const syncStatusText = document.getElementById('sync-status-text');
    
    if (syncScreenerBtn) {
        syncScreenerBtn.onclick = async () => {
            if (!loadingView || !tablesView) return;
            
            syncScreenerBtn.disabled = true;
            const originalBtnText = syncScreenerBtn.innerHTML;
            syncScreenerBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> 同步中...`;
            
            loadingView.style.display = 'flex';
            tablesView.style.display = 'none';
            if (syncStatusText) {
                syncStatusText.innerText = '正在向證交所同步三大法人數據，並下載熱門個股歷史日K線(約150天)，約需30秒，請勿關閉網頁...';
            }
            
            try {
                const res = await fetch('/api/screener/sync', { method: 'POST' });
                const data = await res.json();
                
                if (data.status === 'success') {
                    loadingView.style.display = 'none';
                    tablesView.style.display = 'flex';
                    
                    // 自動加載法人排行
                    await loadInstitutionalRankings();
                    // 同時執行選股篩選
                    await runScreener();
                } else {
                    alert(`同步失敗: ${data.detail || data.message || '未知錯誤'}`);
                    loadingView.style.display = 'none';
                    tablesView.style.display = 'flex';
                }
            } catch (err) {
                console.error("同步數據失敗:", err);
                alert(`連線伺服器失敗: ${err.message}`);
                loadingView.style.display = 'none';
                tablesView.style.display = 'flex';
            } finally {
                syncScreenerBtn.disabled = false;
                syncScreenerBtn.innerHTML = originalBtnText;
            }
        };
    }

    // 0.3 無縫看盤聯動：點擊代號自動切換並看盤
    if (placeholder) {
        placeholder.addEventListener('click', async (e) => {
            const target = e.target.closest('.stock-code-btn');
            if (!target) return;
            
            const code = target.innerText.trim();
            console.log(`[Screener] Select contract code: ${code}`);
            
            const originalText = target.innerText;
            target.innerText = "⏳";
            
            // 顯示載入遮罩
            panes.forEach(p => {
                const l = document.getElementById(`${p.id}-loading`);
                if (l) { l.innerText = `正在切換至 ${code}...`; l.style.display = 'flex'; }
            });
            
            try {
                const res = await fetch('/api/select_contract', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code })
                });
                const data = await res.json();
                
                if (data.status === 'success') {
                    // 重置 Tick 計數器
                    const tickCountEl = document.getElementById('tick-count');
                    if (tickCountEl) tickCountEl.innerText = '0';
                    
                    // 更新所有圖表 Symbol
                    panes.forEach(p => p.symbol = code);
                    
                    // 重新載入所有圖表 K 線
                    await Promise.all(panes.map(p => p.reload()));
                    
                    // 更新全域快照
                    const sRes = await fetch('/api/snapshot');
                    const snap = await sRes.json();
                    globalRefPrice = snap.reference || 0;
                    updateFullUI(snap);
                    
                    // 關鍵：連動更新市場選擇下拉選單，並生成股票選單項目選中當前個股
                    localStorage.setItem('global-market-type', 'stocks');
                    const mSelector = document.getElementById('market-type-selector');
                    if (mSelector) mSelector.value = 'stocks';
                    
                    updateContractSelector('stocks', code);
                    
                    // 關鍵：自動切換回 'chart' 個股分析
                    activeStockTab = 'chart';
                    applyMarket('stocks');
                } else {
                    alert(`切換股票合約失敗: ${data.message}`);
                }
            } catch (err) {
                console.error("切換股票發生錯誤:", err);
                alert(`連線伺服器發生異常: ${err.message}`);
            } finally {
                target.innerText = originalText;
                panes.forEach(p => {
                    const l = document.getElementById(`${p.id}-loading`);
                    if (l) { l.style.display = 'none'; }
                });
            }
        });
    }
    
    // 1. 同步合約選擇器
    const selector = document.getElementById('contract-selector');
    if (selector) {
        selector.value = contractCode;
        selector.onchange = async () => {
            const code = selector.value;
            // 顯示視覺反饋：禁用選單並顯示載入中
            selector.disabled = true;
            panes.forEach(p => {
                const l = document.getElementById(`${p.id}-loading`);
                if (l) { l.innerText = `正在切換至 ${code}...`; l.style.display = 'flex'; }
            });

            try {
                const res = await fetch('/api/select_contract', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ code })
                });
                const data = await res.json();
                if (data.status === 'success') {
                    // 重設計數器與載入數據
                    const tickCountEl = document.getElementById('tick-count');
                    if (tickCountEl) tickCountEl.innerText = '0';
                    
                    // 關鍵修復：切換合約時，更新所有圖表實體的 symbol！
                    panes.forEach(p => p.symbol = code);
                    
                    // 執行重新載入
                    await Promise.all(panes.map(p => p.reload()));
                    
                    // 更新全域快照
                    const sRes = await fetch('/api/snapshot');
                    const snap = await sRes.json();
                    globalRefPrice = snap.reference || 0;
                    updateFullUI(snap);
                }
            } catch (e) {
                console.error("切換失敗", e);
            } finally {
                // 恢復選單並隱藏載入中
                selector.disabled = false;
                panes.forEach(p => {
                    const l = document.getElementById(`${p.id}-loading`);
                    if (l) { l.innerText = "載入中..."; l.style.display = 'none'; }
                });
            }
        };
    }

    // 2. 初始化圖表實體 (預先建立三個，需要時再 init)
    // 全域連動開關記憶
    const syncBtn = document.getElementById('sync-toggle-btn');
    const savedSync = localStorage.getItem('global-sync-enabled');
    if (savedSync !== null) {
        syncBtn.checked = (savedSync === 'true');
    }
    syncBtn.addEventListener('change', () => {
        const enabled = syncBtn.checked;
        localStorage.setItem('global-sync-enabled', enabled);
        
        // 如果關閉連動，立刻強制所有視窗恢復獨立
        if (!enabled && typeof panes !== 'undefined' && Array.isArray(panes)) {
            panes.forEach(p => {
                if (p) {
                    p.isSyncedFocus = false;
                    p.clearSync();
                }
            });
        }
    });

    panes = [new TradingPane('p1'), new TradingPane('p2'), new TradingPane('p3')];
    panes[0].idAttr = 'pane-1';
    panes[1].idAttr = 'pane-2';
    panes[2].idAttr = 'pane-3';
    
    // 關鍵修復：初始化時，將全域當前合約代碼賦予所有圖表實體！
    panes.forEach(p => p.symbol = contractCode);
    
    panes[0].init();

    // 3. 綁定按鈕事件
    const resubBtn = document.getElementById('resub-btn');
    if (resubBtn) {
        resubBtn.onclick = async () => {
            resubBtn.innerText = "⏳ 處理中...";
            try {
                const res = await fetch('/api/resubscribe', { method: 'POST' });
                const data = await res.json();
                if (data.status === 'success') {
                    resubBtn.innerText = "✅ 已重新訂閱";
                    setTimeout(() => resubBtn.innerText = "🔄 重新訂閱", 2000);
                } else {
                    resubBtn.innerText = "🔄 重新訂閱";
                }
            } catch (e) { resubBtn.innerText = "🔄 重新訂閱"; }
        };
    }

    const viewBtn = document.getElementById('view-toggle-btn');
    if (viewBtn) {
        viewBtn.onclick = () => {
            viewMode = (viewMode % 3) + 1;
            const container = document.getElementById('panes-container');
            const p2 = document.getElementById('pane-2');
            const p3 = document.getElementById('pane-3');
            if (!container || !p2 || !p3) return;

            // 清除舊狀態
            container.classList.remove('dual-view', 'triple-view');
            p2.style.display = 'none';
            p3.style.display = 'none';

            if (viewMode === 2) {
                container.classList.add('dual-view');
                p2.style.display = 'flex';
                if (!panes[1].chart) panes[1].init();
            } else if (viewMode === 3) {
                container.classList.add('triple-view');
                p2.style.display = 'flex';
                p3.style.display = 'flex';
                if (!panes[1].chart) panes[1].init();
                if (!panes[2].chart) panes[2].init();
            }

            viewBtn.innerText = `切換視窗 (${viewMode}/3)`;
            panes.forEach(p => p.resize());
        };
    }

    // 4. 初始化 UI 快照
    try {
        const sRes = await fetch('/api/snapshot');
        const snap = await sRes.json();
        updateFullUI(snap);
    } catch (e) { console.error("初始快照失敗", e); }

    // 5. 啟動 WebSocket
    connectWebSocket();

    // 6. 啟動定時補漏 (每 30 秒一次)
    setInterval(async () => {
        // 🎯 股票模式安全哨兵：若當前為股票模式，我們不進行期貨快照輪詢
        const mSelector = document.getElementById('market-type-selector');
        if (mSelector && mSelector.value === 'stocks') return;
        
        try {
            const sRes = await fetch('/api/snapshot');
            const snap = await sRes.json();
            if (snap) updateFullUI(snap);
        } catch (e) {}
    }, 30000);

    // 7. 啟動秒級心跳 (僅針對「非連動中」且「非滑鼠指著」的視窗更新倒數)
    setInterval(() => {
        // 🎯 股票模式安全哨兵：若當前為股票模式，我們不更新圖表心跳
        const mSelector = document.getElementById('market-type-selector');
        if (mSelector && mSelector.value === 'stocks') return;
        
        if (panes && panes.length > 0) {
            panes.forEach(p => {
                if (p && !p.isCrosshairActive && !p.isSyncedFocus) {
                    p.showLatestLegend();
                }
            });
        }
    }, 1000);
    
    // 8. 關鍵修復：等待第一個圖表資料(與所有指標)加載完畢，再淡出並移除 loading 遮罩！
    if (ENABLE_INITIAL_LOADING && initOverlay) {
        (async () => {
            try {
                const loadText = document.getElementById('initial-loading-text');
                if (loadText) loadText.innerText = "正在獲取 K 線歷史數據與計算分析指標...";
                
                // 強制最少顯示 1.5 秒，確保視覺體驗流暢，不會一閃而過 (若沒資料或太快加載時)
                const minWaitPromise = new Promise(resolve => setTimeout(resolve, 1500));
                
                if (panes && panes[0] && panes[0].initPromise) {
                    await Promise.all([panes[0].initPromise, minWaitPromise]);
                } else {
                    await minWaitPromise;
                }
            } catch (e) {
                console.error("啟動數據載入失敗", e);
            } finally {
                initOverlay.style.opacity = '0';
                setTimeout(() => {
                    initOverlay.style.display = 'none';
                    initOverlay.style.opacity = '1'; // 還原供下次使用
                }, 500);
            }
        })();
    } else {
        if (initOverlay) initOverlay.style.display = 'none';
    }
}

function updateFullUI(snap) {
    if (!snap) return;
    // 🎯 股票模式安全哨兵：若當前為股票模式，我們不更新任何期貨欄位，維持乾淨
    const mSelector = document.getElementById('market-type-selector');
    if (mSelector && mSelector.value === 'stocks') return;
    
    const ref = snap.reference || 0;
    const price = snap.close || 0;
    const diff = price - ref;
    const ratio = ref !== 0 ? (diff / ref) * 100 : 0;
    
    // 更新大字報
    const domP = document.getElementById('q-price');
    const domC = document.getElementById('q-change');
    if (domP && domC) {
        domP.innerText = price.toLocaleString();
        let s = diff > 0 ? '▲' : (diff < 0 ? '▼' : '-');
        let cl = diff > 0 ? 'text-up' : (diff < 0 ? 'text-down' : 'text-flat');
        domC.innerText = `${s} ${Math.abs(diff).toLocaleString()} (${diff > 0 ? '+' : ''}${ratio.toFixed(2)}%)`;
        domP.className = `price-huge ${cl}`;
        domC.className = `price-change ${cl}`;
    }
    
    // 更新格點數據
    const fields = {
        'g-open': snap.open,
        'g-high': snap.high,
        'g-low': snap.low,
        'g-ref': ref,
        'g-vol': snap.total_volume || snap.volume
    };
    for (let id in fields) {
        const el = document.getElementById(id);
        if (el) el.innerText = (fields[id] || 0).toLocaleString();
    }
}

const loginBtn = document.getElementById('login-btn');
if (loginBtn) {
    loginBtn.addEventListener('click', async () => {
        loginBtn.disabled = true;
        const msgEl = document.getElementById('login-msg');
        if (msgEl) msgEl.innerText = "連線中...";
        const req = {
            api_key: document.getElementById('api_key').value,
            secret_key: document.getElementById('secret_key').value,
            person_id: document.getElementById('person_id').value,
            is_simulation: document.getElementById('is_simulation').checked,
            ca_path: document.getElementById('ca_path').value,
            ca_passwd: document.getElementById('ca_passwd').value,
            save_keys: document.getElementById('save_keys').checked
        };
        try {
            const res = await fetch('/api/login', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(req)
            });
            const data = await res.json();
            if (res.ok && data.status === 'success') {
                startApp(data.contract);
            } else {
                const errorMsg = data.detail || data.message || "登入流程失敗";
                document.getElementById('login-msg').innerText = "登入失敗: " + errorMsg;
                loginBtn.disabled = false;
            }
        } catch (e) {
            document.getElementById('login-msg').innerText = "伺服器無回應";
            loginBtn.disabled = false;
        }
    });
}

function connectWebSocket() {
    const statusEl = document.getElementById('ws-status');
    let host = window.location.host;
    if (host.includes('localhost')) host = host.replace('localhost', '127.0.0.1');
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${host}/ws`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
        const statusEl = document.getElementById('ws-status');
        if (statusEl) {
            statusEl.innerText = '🟢 連線中';
            statusEl.style.color = '#00FF00';
        }
    };

    ws.onmessage = (event) => {
        // 🎯 股票模式安全哨兵：若當前為股票模式，我們不接收或解析期貨即時 Tick 行情，維持靜默
        const mSelector = document.getElementById('market-type-selector');
        if (mSelector && mSelector.value === 'stocks') return;

        const msg = JSON.parse(event.data);
        if (msg.type === 'tick') {
            const t = msg.data.time, price = msg.data.price;
            
            // 更新計數器
            const tickCountEl = document.getElementById('tick-count');
            if (tickCountEl) {
                const count = parseInt(tickCountEl.innerText) + 1;
                tickCountEl.innerText = count;
            }

            // 更新上方即時跳動數據欄
            const qPriceEl = document.getElementById('q-price');
            if (qPriceEl) qPriceEl.innerText = price.toLocaleString();
            
            if (globalRefPrice > 0) {
                const diff = price - globalRefPrice;
                const pct = (diff / globalRefPrice) * 100;
                
                const domP = document.getElementById('q-price');
                const domC = document.getElementById('q-change');
                if (domP && domC) {
                    domP.innerText = price.toLocaleString();
                    let s = diff > 0 ? '▲' : (diff < 0 ? '▼' : '-');
                    let cl = diff > 0 ? 'text-up' : (diff < 0 ? 'text-down' : 'text-flat');
                    domC.innerText = `${s} ${Math.abs(diff).toLocaleString()} (${diff > 0 ? '+' : ''}${pct.toFixed(2)}%)`;
                    domP.className = `price-huge ${cl}`;
                    domC.className = `price-change ${cl}`;
                }
            }

            const qTime = document.getElementById('q-time');
            if (qTime) qTime.innerText = `最後更新: ${new Date(t*1000).toLocaleTimeString('zh-TW', { hour12: false })}`;
            
            panes.forEach(p => p.onTick(price, t));
        }
    };
    
    ws.onerror = (err) => {
        console.error("WS Error:", err);
    };

    ws.onclose = (e) => {
        if (statusEl) {
            statusEl.innerText = '🔴 斷開連線';
            statusEl.style.color = '#FF0000';
        }
        setTimeout(connectWebSocket, 3000);
    };
}

async function checkStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        if (data.logged_in) {
            startApp(data.contract);
        } else {
            document.getElementById('login-container').style.display = 'block';
            document.getElementById('app-container').style.display = 'none';
            
            // 自動填入已儲存的金鑰
            if (data.env) {
                if (data.env.api_key) document.getElementById('api_key').value = data.env.api_key;
                if (data.env.secret_key) document.getElementById('secret_key').value = data.env.secret_key;
                if (data.env.person_id) document.getElementById('person_id').value = data.env.person_id;
                if (data.env.ca_path) document.getElementById('ca_path').value = data.env.ca_path;
                if (data.env.ca_passwd) document.getElementById('ca_passwd').value = data.env.ca_passwd;
                if (data.env.is_simulation !== undefined) document.getElementById('is_simulation').checked = data.env.is_simulation;
            }
        }
    } catch (e) {
        console.error("狀態檢查失敗", e);
        document.getElementById('login-container').style.display = 'block';
    }
}

window.addEventListener('resize', () => {
    setTimeout(() => {
        if (panes && panes.length > 0) {
            panes.forEach(p => p.resize());
        }
    }, 100);
});

// 監聽視窗焦點：當使用者從別的分頁回來時，立刻刷最新數據補洞
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        console.log("[App] Tab active. Refreshing data to fill gaps...");
        if (panes && panes.length > 0) {
            panes.forEach(p => p.reload());
        }
    }
});

checkStatus();

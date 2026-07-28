let viewMode = 1; // 1: 單圖, 2: 雙圖, 3: 三圖
let panes = [];
let freelancerChartPane = null;
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
        // oldestTime 已減去 28800，加回來才是正確的台灣日期
        const endMs = (this.oldestTime + 28800) * 1000;
        const end = new Date(endMs);
        const days = this.currentPeriod === 'D' ? 365 : 30;
        const start = new Date(endMs);
        start.setUTCDate(start.getUTCDate() - days);
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
            const res = await fetch(`/api/kbars?start=${s}&end=${e}&period=1min`);
            if (!res.ok) {
                const errBody = await res.text();
                console.error(`[kbars] API 錯誤 HTTP ${res.status}:`, errBody);
                return;
            }
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
                if (showLoading) {
                    // 初始載入：直接替換
                    this.kbarsCache = aggregated;
                } else {
                    // loadMore：合併舊有資料，以時間去重
                    const existingTimes = new Set(this.kbarsCache.map(k => k.time));
                    const newBars = aggregated.filter(k => !existingTimes.has(k.time));
                    this.kbarsCache = [...newBars, ...this.kbarsCache];
                }
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
            const macdH = macdEl.clientHeight;
            if (macdH > 0) this.macdChart.resize(w, macdH);
            
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

class FreelancerKChart {
    constructor() {
        this.currentPeriod = document.getElementById('fl-chart-period')?.value || '5min';
        this.chart = null;
        this.candleSeries = null;
        this.kbarsCache = [];
        this.isLoading = false;
        this.symbol = 'TXFR1';
        this.drawTool = 'cursor';
        this.drawings = [];
        this.pendingDrawPoint = null;
        this.previewDrawing = null;
        this.drawingOverlay = null;
    }

    init(symbol = 'TXFR1') {
        this.symbol = symbol || this.symbol;
        const mainEl = document.getElementById('fl-chart-main');
        if (!mainEl || !window.LightweightCharts) return;

        if (this.chart) {
            this.resize();
            return;
        }

        const chartOptions = {
            layout: {
                textColor: '#d6dce8',
                background: { type: 'solid', color: '#11141e' },
                fontSize: 13
            },
            grid: {
                vertLines: { color: '#2b3040', style: LightweightCharts.LineStyle.Dotted },
                horzLines: { color: '#2b3040', style: LightweightCharts.LineStyle.Dotted }
            },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            rightPriceScale: {
                borderColor: '#c9ced8',
                scaleMargins: { top: 0.08, bottom: 0.08 }
            },
            timeScale: {
                borderColor: '#c9ced8',
                timeVisible: true,
                secondsVisible: false,
                tickMarkFormatter: (time) => {
                    if (this.currentPeriod === 'D') {
                        const d = new Date((time + 28800) * 1000);
                        const m = String(d.getUTCMonth() + 1).padStart(2, '0');
                        const day = String(d.getUTCDate()).padStart(2, '0');
                        return `${m}/${day}`;
                    }
                    const d = new Date((time + 28800) * 1000);
                    const hh = String(d.getUTCHours()).padStart(2, '0');
                    const mm = String(d.getUTCMinutes()).padStart(2, '0');
                    return `${hh}:${mm}`;
                }
            },
            localization: {
                timeFormatter: (ts) => {
                    const d = new Date((ts + 28800) * 1000);
                    const y = d.getUTCFullYear();
                    const m = String(d.getUTCMonth() + 1).padStart(2, '0');
                    const day = String(d.getUTCDate()).padStart(2, '0');
                    const hh = String(d.getUTCHours()).padStart(2, '0');
                    const mm = String(d.getUTCMinutes()).padStart(2, '0');
                    return `${y}/${m}/${day} ${hh}:${mm}`;
                }
            }
        };

        this.chart = LightweightCharts.createChart(mainEl, chartOptions);
        this.candleSeries = this.chart.addCandlestickSeries({
            upColor: '#ef554a',
            downColor: '#26a69a',
            borderUpColor: '#ef554a',
            borderDownColor: '#26a69a',
            wickUpColor: '#dfe5ef',
            wickDownColor: '#dfe5ef',
            priceFormat: { type: 'price', precision: 0, minMove: 1 }
        });

        this.chart.subscribeCrosshairMove((param) => {
            const bar = param?.seriesData?.get(this.candleSeries);
            this.updateLegend(bar || this.kbarsCache[this.kbarsCache.length - 1]);
        });
        this.chart.timeScale().subscribeVisibleLogicalRangeChange(() => this.renderDrawings());

        const periodEl = document.getElementById('fl-chart-period');
        if (periodEl) {
            periodEl.addEventListener('change', (e) => {
                this.currentPeriod = e.target.value;
                localStorage.setItem('fl-chart-period', this.currentPeriod);
                this.reload();
            });

            const savedPeriod = localStorage.getItem('fl-chart-period');
            if (savedPeriod) {
                this.currentPeriod = savedPeriod;
                periodEl.value = savedPeriod;
            }
        }

        this.resize();
        this.setupDrawingTools();
        this.reload();
    }

    periodSeconds() {
        if (this.currentPeriod === 'D') return 86400;
        return (parseInt(this.currentPeriod, 10) || 1) * 60;
    }

    async reload() {
        if (!this.candleSeries) return;
        this.kbarsCache = [];
        this.candleSeries.setData([]);
        this.updateLegend(null);

        const days = this.currentPeriod === 'D' ? 180 : 30;
        const end = new Date().toLocaleDateString('en-CA');
        const startDate = new Date(Date.now() - days * 86400000);
        const start = startDate.toLocaleDateString('en-CA');
        await this.fetchData(start, end);
    }

    async fetchData(start, end) {
        this.isLoading = true;
        const loadingEl = document.getElementById('fl-chart-loading');
        if (loadingEl) loadingEl.style.display = 'flex';

        try {
            const res = await fetch(`/api/kbars?start=${start}&end=${end}&period=1min`);
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            this.kbarsCache = this.aggregateBars(data || []);
            this.candleSeries.setData(this.kbarsCache);
            this.updateLegend(this.kbarsCache[this.kbarsCache.length - 1]);
            this.resize();
            if (this.kbarsCache.length > 0) {
                this.chart.timeScale().setVisibleLogicalRange({
                    from: Math.max(0, this.kbarsCache.length - 120),
                    to: this.kbarsCache.length + 8
                });
            }
        } catch (err) {
            console.error('[FreelancerKChart] 載入 K 線失敗:', err);
            const legendEl = document.getElementById('fl-chart-legend');
            if (legendEl) legendEl.innerHTML = '<span style="color:#ff6b6b">K 線載入失敗</span>';
        } finally {
            if (loadingEl) loadingEl.style.display = 'none';
            this.isLoading = false;
        }
    }

    aggregateBars(data) {
        const pSec = this.periodSeconds();
        const result = [];
        let currentBar = null;

        data.forEach(k => {
            const t = Number(k.time);
            const bucketT = Math.floor((t - 1) / pSec) * pSec;
            if (!currentBar || bucketT !== currentBar.time) {
                if (currentBar) result.push(currentBar);
                currentBar = {
                    time: bucketT,
                    open: k.open,
                    high: k.high,
                    low: k.low,
                    close: k.close
                };
            } else {
                currentBar.high = Math.max(currentBar.high, k.high);
                currentBar.low = Math.min(currentBar.low, k.low);
                currentBar.close = k.close;
            }
        });
        if (currentBar) result.push(currentBar);
        return result.sort((a, b) => a.time - b.time);
    }

    onTick(price, time) {
        if (!this.candleSeries || !price || this.isLoading) return;
        const pSec = this.periodSeconds();
        const t = time || Math.floor(Date.now() / 1000);
        const bucketT = Math.floor(t / pSec) * pSec;

        if (this.kbarsCache.length === 0) {
            const firstBar = { time: bucketT, open: price, high: price, low: price, close: price };
            this.kbarsCache.push(firstBar);
            this.candleSeries.setData(this.kbarsCache);
            this.updateLegend(firstBar);
            return;
        }

        const last = this.kbarsCache[this.kbarsCache.length - 1];
        if (bucketT > last.time) {
            const newBar = { time: bucketT, open: price, high: price, low: price, close: price };
            this.kbarsCache.push(newBar);
            this.candleSeries.update(newBar);
            this.updateLegend(newBar);
            this.chart.timeScale().scrollToRealTime();
            return;
        }

        last.close = price;
        last.high = Math.max(last.high, price);
        last.low = Math.min(last.low, price);
        this.candleSeries.update(last);
        this.updateLegend(last);
    }

    updateLegend(bar) {
        const legendEl = document.getElementById('fl-chart-legend');
        if (!legendEl) return;
        if (!bar) {
            legendEl.innerHTML = '<span style="color:#8c94a3">台指期 · TFE</span>';
            return;
        }

        const diff = bar.close - bar.open;
        const pct = bar.open ? (diff / bar.open) * 100 : 0;
        const color = diff >= 0 ? '#00ff55' : '#ff5b5b';
        const sign = diff >= 0 ? '+' : '';
        const periodLabelMap = { '1min': '1', '5min': '5', '15min': '15', '30min': '30', '60min': '60', 'D': '日' };
        const periodLabel = periodLabelMap[this.currentPeriod] || this.currentPeriod;
        legendEl.innerHTML = `
            <span style="font-size:1.05rem;color:#f0f3f8;">台指期 · ${periodLabel} · TFE</span>
            <span style="display:inline-block;margin-left:18px;">開=<span style="color:${color}">${bar.open}</span></span>
            <span>高=<span style="color:${color}">${bar.high}</span></span>
            <span>低=<span style="color:${color}">${bar.low}</span></span>
            <span>收=<span style="color:${color}">${bar.close}</span></span>
            <span style="color:${color}">${sign}${diff.toFixed(0)} (${sign}${pct.toFixed(2)}%)</span>
        `;
    }

    setupDrawingTools() {
        this.drawingOverlay = document.getElementById('fl-drawing-overlay');
        const toolbar = document.getElementById('fl-draw-toolbar');
        if (!this.drawingOverlay || !toolbar) return;

        toolbar.addEventListener('click', (event) => {
            const btn = event.target.closest('.fl-draw-btn');
            if (!btn) return;
            const tool = btn.dataset.tool;
            if (tool === 'disabled') return;
            if (tool === 'clear') {
                this.drawings = [];
                this.pendingDrawPoint = null;
                this.previewDrawing = null;
                this.renderDrawings();
                return;
            }
            this.setDrawTool(tool || 'cursor');
        });

        this.drawingOverlay.addEventListener('click', (event) => this.handleDrawClick(event));
        this.drawingOverlay.addEventListener('mousemove', (event) => this.handleDrawMove(event));
        this.drawingOverlay.addEventListener('mouseleave', () => {
            this.previewDrawing = null;
            this.renderDrawings();
        });
        this.setDrawTool(this.drawTool);
    }

    setDrawTool(tool) {
        this.drawTool = tool;
        this.pendingDrawPoint = null;
        this.previewDrawing = null;
        document.querySelectorAll('.fl-draw-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.tool === tool);
        });
        if (this.drawingOverlay) {
            this.drawingOverlay.classList.toggle('active', tool !== 'cursor');
        }
        this.renderDrawings();
    }

    getDrawPoint(event) {
        if (!this.chart || !this.candleSeries || !this.drawingOverlay) return null;
        const rect = this.drawingOverlay.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        const time = this.chart.timeScale().coordinateToTime(x);
        const price = this.candleSeries.coordinateToPrice(y);
        if (time === null || time === undefined || price === null || price === undefined) return null;
        return { time, price };
    }

    handleDrawClick(event) {
        if (this.drawTool === 'cursor') return;
        event.preventDefault();
        event.stopPropagation();

        const point = this.getDrawPoint(event);
        if (!point) return;

        if (this.drawTool === 'horizontal') {
            this.drawings.push({ type: 'horizontal', price: point.price });
            this.renderDrawings();
            return;
        }

        if (this.drawTool === 'vertical') {
            this.drawings.push({ type: 'vertical', time: point.time });
            this.renderDrawings();
            return;
        }

        if (!this.pendingDrawPoint) {
            this.pendingDrawPoint = point;
            return;
        }

        this.drawings.push({
            type: this.drawTool,
            start: this.pendingDrawPoint,
            end: point
        });
        this.pendingDrawPoint = null;
        this.previewDrawing = null;
        this.renderDrawings();
    }

    handleDrawMove(event) {
        if (!this.pendingDrawPoint || (this.drawTool !== 'trendline' && this.drawTool !== 'rect')) return;
        const point = this.getDrawPoint(event);
        if (!point) return;
        this.previewDrawing = {
            type: this.drawTool,
            start: this.pendingDrawPoint,
            end: point,
            preview: true
        };
        this.renderDrawings();
    }

    pointToCoordinate(point) {
        const x = this.chart.timeScale().timeToCoordinate(point.time);
        const y = this.candleSeries.priceToCoordinate(point.price);
        if (x === null || y === null) return null;
        return { x, y };
    }

    renderDrawings() {
        if (!this.drawingOverlay || !this.chart || !this.candleSeries) return;
        const width = this.drawingOverlay.clientWidth;
        const height = this.drawingOverlay.clientHeight;
        this.drawingOverlay.setAttribute('viewBox', `0 0 ${width} ${height}`);
        this.drawingOverlay.innerHTML = '';

        [...this.drawings, this.previewDrawing].filter(Boolean).forEach(shape => {
            if (shape.type === 'horizontal') {
                const y = this.candleSeries.priceToCoordinate(shape.price);
                if (y !== null) this.addSvgLine(0, y, width, y, shape.preview);
                return;
            }

            if (shape.type === 'vertical') {
                const x = this.chart.timeScale().timeToCoordinate(shape.time);
                if (x !== null) this.addSvgLine(x, 0, x, height, shape.preview);
                return;
            }

            const start = this.pointToCoordinate(shape.start);
            const end = this.pointToCoordinate(shape.end);
            if (!start || !end) return;

            if (shape.type === 'rect') {
                this.addSvgRect(start, end, shape.preview);
            } else {
                this.addSvgLine(start.x, start.y, end.x, end.y, shape.preview);
            }
        });
    }

    addSvgLine(x1, y1, x2, y2, preview = false) {
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', x1);
        line.setAttribute('y1', y1);
        line.setAttribute('x2', x2);
        line.setAttribute('y2', y2);
        line.setAttribute('stroke', preview ? '#9bbcff' : '#4facfe');
        line.setAttribute('stroke-width', '2');
        line.setAttribute('stroke-dasharray', preview ? '6 5' : '');
        this.drawingOverlay.appendChild(line);
    }

    addSvgRect(start, end, preview = false) {
        const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
        rect.setAttribute('x', Math.min(start.x, end.x));
        rect.setAttribute('y', Math.min(start.y, end.y));
        rect.setAttribute('width', Math.abs(end.x - start.x));
        rect.setAttribute('height', Math.abs(end.y - start.y));
        rect.setAttribute('fill', preview ? 'rgba(79,172,254,0.08)' : 'rgba(79,172,254,0.12)');
        rect.setAttribute('stroke', preview ? '#9bbcff' : '#4facfe');
        rect.setAttribute('stroke-width', '2');
        rect.setAttribute('stroke-dasharray', preview ? '6 5' : '');
        this.drawingOverlay.appendChild(rect);
    }

    resize() {
        const mainEl = document.getElementById('fl-chart-main');
        if (!mainEl || !this.chart) return;
        const width = mainEl.clientWidth;
        const height = mainEl.clientHeight;
        if (width > 0 && height > 0) {
            this.chart.resize(width, height);
            this.renderDrawings();
        }
    }
}

function ensureFreelancerChart(symbol = 'TXFR1') {
    if (!freelancerChartPane) {
        freelancerChartPane = new FreelancerKChart();
        freelancerChartPane.init(symbol);
        return;
    }
    freelancerChartPane.symbol = symbol || freelancerChartPane.symbol;
    freelancerChartPane.resize();
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
        const _rawText1 = await res.text();
        let data;
        try { data = JSON.parse(_rawText1); } catch(_e) {
            throw new Error(`三大法人 API 回傳非 JSON：${_rawText1.slice(0, 120)}`);
        }
        
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

// ── 全域選股數據與 Tab 篩選狀態 ──────────────────────────────────────────
let _screenerAllData = [];
let _activeStrategyTab = 'all';
let _marketStatus = null;   // 最新大盤狀態物件

function _getStrategyStateBadgeStyle(state) {
    const map = {
        '明日優先': { bg: 'rgba(38,222,129,0.15)', color: '#26de81', border: 'rgba(38,222,129,0.4)' },
        '突破觀察': { bg: 'rgba(79,172,254,0.15)', color: '#4facfe', border: 'rgba(79,172,254,0.4)' },
        '等回測':   { bg: 'rgba(255,211,51,0.15)',  color: '#ffd233', border: 'rgba(255,211,51,0.4)' },
        '過熱警戒': { bg: 'rgba(255,68,68,0.15)',   color: '#ff4444', border: 'rgba(255,68,68,0.4)' },
    };
    return map[state] || { bg: 'rgba(150,150,150,0.1)', color: '#888', border: 'rgba(150,150,150,0.3)' };
}

function _getStopLossColor(pct) {
    const abs = Math.abs(pct);
    if (abs === 0) return '#666';
    if (abs <= 4) return '#26de81';
    if (abs <= 6) return '#ffd233';
    if (abs <= 8) return '#ff9f43';
    return '#ff4444';
}

function _renderMarketStatusCard(ms) {
    const card = document.getElementById('market-status-card');
    if (!card) return;
    if (!ms) { card.style.display = 'none'; return; }

    const statusMap = {
        normal_bull:     { emoji: '🟢', color: '#26de81', bg: 'rgba(38,222,129,0.08)', border: 'rgba(38,222,129,0.3)' },
        hot_bull:        { emoji: '🟡', color: '#ffd233', bg: 'rgba(255,210,51,0.08)',  border: 'rgba(255,210,51,0.3)' },
        overheated_bull: { emoji: '🔴', color: '#ff4444', bg: 'rgba(255,68,68,0.08)',   border: 'rgba(255,68,68,0.3)' },
        weak_market:     { emoji: '⚪', color: '#888',    bg: 'rgba(150,150,150,0.06)', border: 'rgba(150,150,150,0.25)' },
    };
    const st  = statusMap[ms.status] || statusMap.normal_bull;
    const m   = ms.metrics || {};
    const marginText = m.margin_5d_change != null
        ? `${(m.margin_5d_change / 1e8).toFixed(0)} 億`
        : '資料不足';

    card.style.display    = 'block';
    card.style.background = st.bg;
    card.style.border     = `1px solid ${st.border}`;
    card.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
            <span style="font-size:1.05rem;font-weight:bold;color:${st.color};">
                ${st.emoji} 目前市場狀態：${ms.label}
            </span>
            <span style="color:#888;font-size:0.78rem;">${ms.description}</span>
        </div>
        <div style="display:flex;gap:18px;flex-wrap:wrap;margin-top:8px;font-size:0.8rem;color:#aaa;">
            <span>大盤 <b style="color:#fff;">${(m.index_close||0).toLocaleString()}</b></span>
            <span>20MA <b style="color:#fff;">${(m.index_ma20||0).toLocaleString()}</b></span>
            <span>60MA <b style="color:#fff;">${(m.index_ma60||0).toLocaleString()}</b></span>
            <span>距20MA <b style="color:${Math.abs(m.bias_ma20_pct||0)>6?'#ffd233':'#26de81'};">${(m.bias_ma20_pct||0)>0?'+':''}${(m.bias_ma20_pct||0).toFixed(1)}%</b></span>
            <span>距60MA <b style="color:${Math.abs(m.bias_ma60_pct||0)>12?'#ffd233':'#26de81'};">${(m.bias_ma60_pct||0)>0?'+':''}${(m.bias_ma60_pct||0).toFixed(1)}%</b></span>
            <span>過熱個股 <b style="color:${(m.hot_stock_ratio||0)>20?'#ffd233':'#aaa'};">${(m.hot_stock_ratio||0).toFixed(0)}%</b></span>
            <span>融資5日變化 <b style="color:#aaa;">${marginText}</b></span>
        </div>
        <div style="margin-top:8px;font-size:0.8rem;color:#ccc;">
            <span style="color:${st.color};font-weight:bold;">操作建議：</span>${ms.suggestion}
        </div>`;
}

function _renderScreenerRows(list) {
    const resultsBody = document.getElementById('screener-results-body');
    if (!resultsBody) return;

    if (list.length === 0) {
        resultsBody.innerHTML = `<tr><td colspan="14" style="text-align:center;color:#888;padding:50px;"><i class="fas fa-info-circle" style="font-size:1.5rem;color:#555;margin-bottom:10px;display:block;"></i>目前條件下無符合的候選股票。</td></tr>`;
        return;
    }

    resultsBody.innerHTML = list.map((item, idx) => {
        // 相容新舊欄位名稱
        const stockCode  = item.stockCode  || item.code  || '--';
        const stockName  = item.stockName  || item.name  || '--';
        const closePrice = item.closePrice || item.close || 0;
        const score      = item.score      !== undefined ? item.score : (item.priority || 0);
        const s          = item.strategyState || '';
        const sLabel     = item.strategyStateLabel || s;
        const slPrice    = item.stopLossPrice    !== undefined ? item.stopLossPrice    : 0;
        const slPct      = item.stopLossPercent  !== undefined ? item.stopLossPercent  : 0;

        const style = _getStrategyStateBadgeStyle(s);
        const strategyBadge = s
            ? `<span style="background:${style.bg};color:${style.color};border:1px solid ${style.border};padding:3px 8px;border-radius:4px;font-size:0.72rem;font-weight:bold;white-space:nowrap;">${sLabel}</span>`
            : `<span style="color:#555;font-size:0.72rem;">--</span>`;

        const entryLabel = item.entryPatternLabel || item.entryPattern || '--';
        let entryColor = '#ff9f43';
        if (entryLabel.includes('回測20MA') || entryLabel.includes('回測前高')) entryColor = '#4facfe';
        if (entryLabel.includes('突破') || entryLabel.includes('高檔')) entryColor = '#26de81';
        if (entryLabel.includes('等回測') || entryLabel.includes('過熱')) entryColor = '#888';
        const entryBadge = `<span style="color:${entryColor};font-size:0.75rem;white-space:nowrap;">${entryLabel}</span>`;

        const bias = item.bias20 !== undefined ? item.bias20 : (item.bias || 0);
        const biasColor = bias >= 10 ? '#ffd233' : '#26de81';
        const biasText = `${bias > 0 ? '+' : ''}${bias}%${bias >= 10 ? ' ⚠️' : ''}`;

        const r20 = item.return20 !== undefined ? item.return20 : (item.gain_20 || 0);
        const r60 = item.return60 !== undefined ? item.return60 : (item.gain_60 || 0);
        const gain20Color = r20 >= 0 ? '#ff4444' : '#44ff44';
        const gain60Color = r60 >= 0 ? '#ff4444' : '#44ff44';

        const slColor = _getStopLossColor(slPct);
        let slText = '--';
        if (slPrice > 0) {
            const abs = Math.abs(slPct);
            const level = abs <= 4 ? '低風險' : abs <= 6 ? '可接受' : abs <= 8 ? '偏高' : '不建議';
            slText = `<span style="color:${slColor};font-weight:bold;">${slPct.toFixed(1)}%</span><br><span style="color:#666;font-size:0.68rem;">${level}</span>`;
        } else if (s === '過熱警戒' || s === '等回測') {
            slText = `<span style="color:#555;font-size:0.72rem;">不計算</span>`;
        }

        const scoreColor = score >= 80 ? '#26de81' : score >= 65 ? '#4facfe' : score >= 50 ? '#ffd233' : '#888';

        // 法人特徵 badges
        let badges = '';
        if (item.tier_level === 1) badges += `<span style="background:linear-gradient(135deg,rgba(255,215,0,0.2),rgba(255,165,0,0.2));color:#ffd700;border:1px solid rgba(255,215,0,0.4);padding:2px 6px;border-radius:3px;font-size:0.68rem;font-weight:bold;margin-right:3px;white-space:nowrap;">👑 黃金滿貫</span>`;
        else if (item.tier_level === 2) badges += `<span style="background:rgba(165,94,234,0.15);color:#e056fd;border:1px solid rgba(165,94,234,0.4);padding:2px 6px;border-radius:3px;font-size:0.68rem;font-weight:bold;margin-right:3px;white-space:nowrap;">🥈 強勢雙雄</span>`;
        if (item.sync_buy) badges += `<span style="background:rgba(255,68,68,0.12);color:#ff4444;border:1px solid rgba(255,68,68,0.3);padding:2px 5px;border-radius:3px;font-size:0.68rem;font-weight:bold;margin-right:3px;white-space:nowrap;">🔥 三人同買</span>`;
        if (item.investment_strike > 0) badges += `<span style="background:rgba(255,159,67,0.12);color:#ff9f43;border:1px solid rgba(255,159,67,0.3);padding:2px 5px;border-radius:3px;font-size:0.68rem;margin-right:3px;white-space:nowrap;">投信連買${item.investment_strike}D</span>`;
        if (item.foreign_strike > 0) badges += `<span style="background:rgba(79,172,254,0.12);color:#4facfe;border:1px solid rgba(79,172,254,0.3);padding:2px 5px;border-radius:3px;font-size:0.68rem;margin-right:3px;white-space:nowrap;">外資連買${item.foreign_strike}D</span>`;
        if (!badges) badges = `<span style="color:#555;font-size:0.72rem;">主力溫和佈局</span>`;

        const rowBg = idx % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.015)';

        // 建議買法欄
        const bm = item.buy_method || {};
        const bmAllowed  = bm.allowed;
        const bmLabel    = bm.label || '--';
        const bmReason   = bm.reason || '';
        let bmColor = bmAllowed ? '#26de81' : '#888';
        if (bmAllowed === false && (bm.action === 'no_trade')) bmColor = '#ff4444';
        const bmBadge = `<span style="color:${bmColor};font-size:0.72rem;white-space:nowrap;" title="${bmReason}">${bmLabel}</span>`;
        const allowedBadge = bmAllowed === true
            ? `<span style="color:#26de81;font-size:0.85rem;" title="允許交易">✅</span>`
            : bmAllowed === false
                ? `<span style="color:#ff4444;font-size:0.85rem;" title="${bmReason}">🚫</span>`
                : `<span style="color:#666;font-size:0.8rem;">--</span>`;

        return `<tr class="screener-row" data-idx="${idx}" style="cursor:pointer;background:${rowBg};transition:background 0.15s;${bmAllowed===false?'opacity:0.7;':''}" onmouseover="this.style.background='rgba(79,172,254,0.06)'" onmouseout="this.style.background='${rowBg}'">
            <td style="padding:8px 4px;"><span style="color:#4facfe;font-family:monospace;font-weight:bold;font-size:0.85rem;">${stockCode}</span></td>
            <td style="padding:8px 4px;color:#fff;font-size:0.8rem;font-weight:bold;">${stockName}</td>
            <td style="padding:8px 4px;text-align:center;">${strategyBadge}</td>
            <td style="padding:8px 4px;text-align:center;font-weight:bold;color:${scoreColor};font-size:0.85rem;">${score}</td>
            <td style="padding:8px 4px;text-align:right;font-weight:600;color:#fff;">${(+closePrice||0).toFixed(2)}</td>
            <td style="padding:8px 4px;text-align:right;color:${biasColor};font-weight:bold;">${biasText}</td>
            <td style="padding:8px 4px;text-align:right;color:${gain20Color};font-weight:500;">${r20 > 0 ? '+' : ''}${r20}%</td>
            <td style="padding:8px 4px;text-align:right;color:${gain60Color};font-weight:500;">${r60 > 0 ? '+' : ''}${r60}%</td>
            <td style="padding:8px 4px;text-align:right;color:#ff9f43;font-weight:600;" title="${item.highInstRatioWarning ? '法人佔比偏高，請確認成交金額與流動性' : ''}">${item.institutionBuyRatio5 || item.inst_ratio_5d || 0}%${item.highInstRatioWarning ? ' <span style="color:#ffd233;cursor:help;" title="法人佔比偏高，請確認成交金額與流動性">⚠️</span>' : ''}</td>
            <td style="padding:8px 4px;text-align:center;">${entryBadge}</td>
            <td style="padding:8px 4px;text-align:center;">${bmBadge}</td>
            <td style="padding:8px 4px;text-align:center;">${allowedBadge}</td>
            <td style="padding:8px 4px;text-align:right;line-height:1.4;">${slText}</td>
            <td style="padding:8px 4px;">${badges}</td>
        </tr>`;
    }).join('');

    // 綁定 row 點擊事件 → 開啟 Drawer
    document.querySelectorAll('.screener-row').forEach(row => {
        row.addEventListener('click', () => {
            const idx = parseInt(row.dataset.idx);
            const filtered = _activeStrategyTab === 'all' ? _screenerAllData : _screenerAllData.filter(i => i.strategyState === _activeStrategyTab);
            if (filtered[idx]) openStockDrawer(filtered[idx]);
        });
    });
}

function _updateStrategyTabs(list) {
    const counts = { '明日優先': 0, '突破觀察': 0, '等回測': 0, '過熱警戒': 0 };
    list.forEach(item => {
        if (counts[item.strategyState] !== undefined) counts[item.strategyState]++;
    });
    const allEl = document.getElementById('tab-count-all');
    if (allEl) allEl.textContent = list.length;
    Object.entries(counts).forEach(([state, cnt]) => {
        const el = document.getElementById(`tab-count-${state}`);
        if (el) el.textContent = cnt;
    });
}

function _applyStrategyTab(state) {
    _activeStrategyTab = state;
    document.querySelectorAll('.strategy-tab-btn').forEach(btn => {
        const isActive = btn.dataset.state === state;
        btn.classList.toggle('active', isActive);
        btn.style.opacity = isActive ? '1' : '0.55';
        btn.style.fontWeight = isActive ? 'bold' : '500';
    });
    const filtered = state === 'all' ? _screenerAllData : _screenerAllData.filter(i => i.strategyState === state);
    _renderScreenerRows(filtered);
}

// ── Drawer 函數 ────────────────────────────────────────────────────────────
function openStockDrawer(item) {
    const drawer = document.getElementById('stock-detail-drawer');
    const overlay = document.getElementById('drawer-overlay');
    if (!drawer) return;

    // 相容新舊欄位名稱（server 可能返回舊版或新版欄位）
    const stockCode  = item.stockCode  || item.code  || '--';
    const stockName  = item.stockName  || item.name  || '--';
    const closeP     = +(item.closePrice || item.close || 0);
    const score      = item.score      !== undefined ? item.score : (item.priority || 0);
    const s          = item.strategyState || '';
    const bias       = item.bias20     !== undefined ? item.bias20 : (item.bias || 0);
    const r20        = item.return20   !== undefined ? item.return20 : (item.gain_20 || 0);
    const r60        = item.return60   !== undefined ? item.return60 : (item.gain_60 || 0);
    const instRatio  = item.institutionBuyRatio5 !== undefined ? item.institutionBuyRatio5 : (item.inst_ratio_5d || 0);
    const fStreak    = item.foreignConsecutiveBuyDays !== undefined ? item.foreignConsecutiveBuyDays : (item.foreign_strike || 0);
    const itStreak   = item.investmentTrustConsecutiveBuyDays !== undefined ? item.investmentTrustConsecutiveBuyDays : (item.investment_strike || 0);

    // 標題
    document.getElementById('drawer-stock-title').textContent = `${stockName}（${stockCode}）`;
    document.getElementById('drawer-stock-sub').textContent = `資料日期：最新收盤`;
    const industryBadge = document.getElementById('drawer-industry-badge');
    const industry = item.industry || '';
    if (industry) {
        industryBadge.textContent = industry;
        industryBadge.style.display = 'inline-block';
    } else {
        industryBadge.style.display = 'none';
    }

    // 基本資訊
    document.getElementById('drawer-close-price').textContent = closeP.toFixed(2);
    const chg = item.todayChangePercent || 0;
    const chgEl = document.getElementById('drawer-change-pct');
    chgEl.textContent = `${chg > 0 ? '+' : ''}${chg.toFixed(2)}%`;
    chgEl.style.color = chg > 0 ? '#ff4444' : chg < 0 ? '#44ff44' : '#888';
    document.getElementById('drawer-ma20').textContent = (item.ma20 || 0).toFixed(2);
    const biasEl2 = document.getElementById('drawer-bias20');
    biasEl2.textContent = `${bias > 0 ? '+' : ''}${bias}%`;
    biasEl2.style.color = bias >= 10 ? '#ffd233' : '#26de81';

    // 策略判斷
    const sStyle = _getStrategyStateBadgeStyle(s);
    const sBadge = document.getElementById('drawer-strategy-badge');
    sBadge.textContent = item.strategyStateLabel || s || '--';
    sBadge.style.background = sStyle.bg;
    sBadge.style.color = sStyle.color;
    sBadge.style.borderColor = sStyle.border;
    document.getElementById('drawer-entry-badge').textContent = item.entryPatternLabel || item.entryPattern || '--';
    document.getElementById('drawer-score-badge').textContent = `分數 ${score}`;
    document.getElementById('drawer-strategy-reason').textContent = item.strategyReason || '無說明';

    // 技術條件
    const checks = [
        { label: '收盤價 > 20MA > 60MA', pass: closeP > (item.ma20||0) && (item.ma20||0) > (item.ma60||0) },
        { label: '近20日強於大盤 (+1.5%)', pass: r20 > 1.5 },
        { label: '近60日強於大盤 (+4.0%)', pass: r60 > 4.0 },
        { label: '乖離20MA < 10% (安全區)', pass: bias < 10 },
        { label: 'MACD 負柱連續收斂', pass: (item.macdHistogram||0) < 0 && (item.macdHistogram||0) > (item.macdHistogramPrev1||0) && (item.macdHistogramPrev1||0) > (item.macdHistogramPrev2||0) },
        { label: '今日收紅K / 站回短均線', pass: closeP > (item.openPrice||0) || closeP > (item.ma5||0) || closeP > (item.ma10||0) },
    ];
    document.getElementById('drawer-tech-checks').innerHTML = checks.map(c =>
        `<div style="display:flex;align-items:center;gap:8px;padding:5px 8px;background:rgba(255,255,255,0.02);border-radius:4px;">
            <span style="color:${c.pass ? '#26de81' : '#555'};font-size:0.9rem;">${c.pass ? '✓' : '✗'}</span>
            <span style="color:${c.pass ? '#ccc' : '#555'};">${c.label}</span>
        </div>`
    ).join('');

    // 法人籌碼
    const fmtInt = v => v > 0 ? `+${v}張` : v < 0 ? `${v}張` : '0';
    document.getElementById('drawer-foreign').textContent = fmtInt(item.foreignBuy5 || 0);
    document.getElementById('drawer-it').textContent = fmtInt(item.investmentTrustBuy5 || 0);
    document.getElementById('drawer-total-inst').textContent = fmtInt(item.totalInstitutionBuy5 || 0);
    document.getElementById('drawer-foreign-streak').textContent = fStreak;
    document.getElementById('drawer-it-streak').textContent = itStreak;
    document.getElementById('drawer-inst-ratio').textContent = instRatio.toFixed(2);
    const instWarningEl = document.getElementById('drawer-inst-ratio-warning');
    if (instWarningEl) {
        if (item.highInstRatioWarning) {
            instWarningEl.innerHTML = `<div style="background:rgba(255,211,51,0.1);border-left:3px solid #ffd233;border-radius:4px;padding:7px 10px;color:#ffd233;font-size:0.74rem;">⚠️ 法人佔比偏高，請確認成交金額與流動性<br><span style="color:#888;font-size:0.7rem;line-height:1.5;">法人佔比過高可能代表籌碼集中，也可能是成交金額較小導致比例被放大。</span></div>`;
            instWarningEl.style.display = 'block';
        } else {
            instWarningEl.innerHTML = '';
            instWarningEl.style.display = 'none';
        }
    }

    // 明日操作計畫
    const plan = item.actionPlan || {};
    const prevHigh = item.previousHighPrice || 0;
    const ma5v     = item.ma5  || 0;
    const ma10v    = item.ma10 || 0;
    const ma20v    = item.ma20 || 0;
    const slPriceV = item.stopLossPrice || 0;
    const slPctV   = item.stopLossPercent || 0;

    // 關鍵價位格格
    const priceGridItems = [
        { label: '前日高點', value: prevHigh > 0 ? prevHigh.toFixed(2) : '--', color: '#ff9f43' },
        { label: '5MA',      value: ma5v  > 0 ? ma5v.toFixed(2)  : '--', color: '#FFFF00' },
        { label: '10MA',     value: ma10v > 0 ? ma10v.toFixed(2) : '--', color: '#00FFFF' },
        { label: '20MA',     value: ma20v > 0 ? ma20v.toFixed(2) : '--', color: '#B200FF' },
        { label: '停損價',   value: slPriceV > 0 ? slPriceV.toFixed(2) : '--', color: '#ff4444' },
        { label: '停損距離', value: slPriceV > 0 ? `${slPctV.toFixed(2)}%` : '--', color: _getStopLossColor(slPctV) },
    ];
    const priceGridHtml = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px;">
        ${priceGridItems.map(p => `<div style="background:#1a1a1a;padding:6px 8px;border-radius:6px;text-align:center;">
            <div style="color:#666;font-size:0.65rem;margin-bottom:2px;">${p.label}</div>
            <div style="color:${p.color};font-weight:bold;font-size:0.82rem;">${p.value}</div>
        </div>`).join('')}
    </div>`;

    // 操作計畫區塊
    const hasNewFormat = !!(plan.conservative || plan.aggressive || plan.avoid);
    let planBlockHtml = '';
    if (plan.strategy) {
        planBlockHtml += `<div style="background:rgba(79,172,254,0.08);padding:8px 12px;border-radius:6px;border-left:3px solid #4facfe;margin-bottom:6px;">
            <div style="color:#4facfe;font-size:0.7rem;font-weight:bold;margin-bottom:3px;">明日策略</div>
            <div style="color:#ccc;line-height:1.5;font-weight:600;">${plan.strategy}</div>
        </div>`;
    }
    if (hasNewFormat) {
        const blocks = [
            { label: '🔵 保守進場', value: plan.conservative, color: '#26de81', bg: 'rgba(38,222,129,0.06)' },
            { label: '🚀 積極進場', value: plan.aggressive,   color: '#ff9f43', bg: 'rgba(255,159,67,0.06)' },
            { label: '⚠️ 不進場條件', value: plan.avoid,     color: '#ffd233', bg: 'rgba(255,211,51,0.06)' },
            { label: '🛡 停損條件',  value: plan.stopLoss,    color: '#ff4444', bg: 'rgba(255,68,68,0.06)'  },
        ];
        planBlockHtml += blocks.filter(b => b.value && b.value !== '無進場規劃').map(b =>
            `<div style="background:${b.bg};padding:8px 12px;border-radius:6px;border-left:3px solid ${b.color};margin-bottom:5px;">
                <div style="color:${b.color};font-size:0.7rem;font-weight:bold;margin-bottom:3px;">${b.label}</div>
                <div style="color:#ccc;line-height:1.5;">${b.value}</div>
            </div>`
        ).join('');
    } else {
        // 舊格式相容
        const legacyItems = [
            { label: '觸發條件', value: plan.trigger,  color: '#26de81', bg: 'rgba(38,222,129,0.06)' },
            { label: '停損條件', value: plan.stopLoss,  color: '#ff4444', bg: 'rgba(255,68,68,0.06)'  },
            { label: '注意事項', value: plan.notes,     color: '#ffd233', bg: 'rgba(255,211,51,0.06)' },
        ];
        planBlockHtml += legacyItems.filter(b => b.value).map(b =>
            `<div style="background:${b.bg};padding:8px 12px;border-radius:6px;border-left:3px solid ${b.color};margin-bottom:5px;">
                <div style="color:${b.color};font-size:0.7rem;font-weight:bold;margin-bottom:3px;">${b.label}</div>
                <div style="color:#ccc;line-height:1.5;">${b.value}</div>
            </div>`
        ).join('');
    }
    document.getElementById('drawer-action-plan').innerHTML = priceGridHtml + (planBlockHtml || '<div style="color:#555;text-align:center;padding:8px;">無操作計畫</div>');

    // 建議買法（market status aware）
    const bm = item.buy_method || {};
    const drawerBmEl = document.getElementById('drawer-buy-method');
    if (drawerBmEl) {
        if (bm.label) {
            const bmAllowed = bm.allowed;
            const bmColor = bmAllowed ? '#26de81' : (bm.action === 'no_trade' ? '#ff4444' : '#888');
            const allowTag = bmAllowed ? '✅ 允許交易' : '🚫 不建議交易';
            const allowBg = bmAllowed ? 'rgba(38,222,129,0.1)' : 'rgba(255,68,68,0.08)';
            const allowBorder = bmAllowed ? 'rgba(38,222,129,0.3)' : 'rgba(255,68,68,0.25)';
            const positionSugg = bm.position_suggestion || '';
            drawerBmEl.style.display = 'block';
            drawerBmEl.innerHTML = `
                <div style="color:#666; font-size:0.7rem; letter-spacing:1px; margin-bottom:10px;">建議買法</div>
                <div style="background:${allowBg}; border:1px solid ${allowBorder}; border-radius:8px; padding:12px 14px;">
                    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
                        <span style="color:${bmColor}; font-weight:bold; font-size:0.88rem;">${bm.label}</span>
                        <span style="color:${bmColor}; font-size:0.75rem; font-weight:bold;">${allowTag}</span>
                    </div>
                    ${bm.reason ? `<div style="color:#888; font-size:0.72rem; margin-bottom:8px; line-height:1.5;">${bm.reason}</div>` : ''}
                    ${bm.entry_condition ? `<div style="margin-bottom:6px;"><span style="color:#666; font-size:0.7rem;">進場條件：</span><span style="color:#ccc; font-size:0.78rem;">${bm.entry_condition}</span></div>` : ''}
                    ${bm.stop_loss_rule ? `<div style="margin-bottom:6px;"><span style="color:#666; font-size:0.7rem;">停損規則：</span><span style="color:#ff9f43; font-size:0.78rem;">${bm.stop_loss_rule}</span></div>` : ''}
                    ${positionSugg ? `<div style="margin-top:8px; padding:6px 10px; background:rgba(255,255,255,0.04); border-radius:4px; color:#aaa; font-size:0.75rem;">${positionSugg}</div>` : ''}
                </div>`;
        } else {
            drawerBmEl.style.display = 'none';
        }
    }

    // 停損資訊
    const slP = item.stopLossPrice || 0;
    const slPct = item.stopLossPercent || 0;
    document.getElementById('drawer-sl-price').textContent = slP > 0 ? slP.toFixed(2) : '--';
    const slPctEl = document.getElementById('drawer-sl-pct');
    if (slP > 0) {
        slPctEl.textContent = `${slPct.toFixed(2)}%`;
        slPctEl.style.color = _getStopLossColor(slPct);
    } else {
        slPctEl.textContent = '--';
        slPctEl.style.color = '#555';
    }
    const absSlPct = Math.abs(slPct);
    const slLevelEl = document.getElementById('drawer-sl-level');
    if (slP > 0) {
        const levelMap = [
            { max: 4,  label: '低風險',  bg: 'rgba(38,222,129,0.15)',  color: '#26de81' },
            { max: 6,  label: '可接受',  bg: 'rgba(255,211,51,0.15)',  color: '#ffd233' },
            { max: 8,  label: '偏高，降低優先度', bg: 'rgba(255,159,67,0.15)', color: '#ff9f43' },
            { max: 999,label: '不建議追價', bg: 'rgba(255,68,68,0.15)', color: '#ff4444' },
        ];
        const lv = levelMap.find(l => absSlPct <= l.max);
        slLevelEl.textContent = `停損距離 ${slPct.toFixed(1)}%：${lv.label}`;
        slLevelEl.style.background = lv.bg;
        slLevelEl.style.color = lv.color;
    } else {
        slLevelEl.textContent = '';
    }

    // 分數明細
    const breakdown = item.scoreBreakdown || [];
    const scoreTotalBadge = document.getElementById('drawer-score-total-badge');
    const breakdownEl = document.getElementById('drawer-score-breakdown');
    if (scoreTotalBadge) scoreTotalBadge.textContent = `總分 ${score}`;
    if (breakdownEl) {
        if (breakdown.length === 0) {
            breakdownEl.innerHTML = '<div style="color:#555; text-align:center; padding:12px;">無分數明細資料</div>';
        } else {
            const positives = breakdown.filter(b => b.delta >= 0 || (b.passed && b.delta > 0));
            const penalties = breakdown.filter(b => b.delta < 0 && b.passed);
            const notPassed = breakdown.filter(b => !b.passed && b.delta >= 0);

            const renderItem = (b) => {
                const isBonus   = b.delta > 0 && b.passed;
                const isPenalty = b.delta < 0 && b.passed;
                const isMissed  = !b.passed && b.delta >= 0;
                const icon      = isBonus ? '✅' : isPenalty ? '❌' : '–';
                const deltaColor = isBonus ? '#26de81' : isPenalty ? '#ff4444' : '#555';
                const labelColor = (isBonus || isPenalty) ? '#ccc' : '#555';
                const deltaText  = b.delta > 0 ? `+${b.delta}` : `${b.delta}`;
                return `<div style="display:grid; grid-template-columns:18px 1fr 36px; gap:4px; align-items:start; padding:5px 8px; background:rgba(255,255,255,0.02); border-radius:4px; ${isPenalty ? 'border-left:2px solid rgba(255,68,68,0.4);' : ''}">
                    <span style="font-size:0.8rem; line-height:1.5;">${icon}</span>
                    <div>
                        <span style="color:${labelColor}; font-weight:${isBonus || isPenalty ? '600' : '400'};">${b.label}</span>
                        ${b.detail ? `<div style="color:#666; font-size:0.7rem; margin-top:1px; line-height:1.4;">${b.detail}</div>` : ''}
                    </div>
                    <span style="color:${deltaColor}; font-weight:bold; text-align:right; line-height:1.5;">${(isBonus || isPenalty) ? deltaText : (isMissed ? `+0` : '')}</span>
                </div>`;
            };

            let html = '';
            if (positives.length + notPassed.length > 0) {
                html += `<div style="color:#26de81; font-size:0.7rem; font-weight:bold; letter-spacing:1px; margin:6px 0 4px;">加分項目</div>`;
                html += [...positives, ...notPassed].map(renderItem).join('');
            }
            if (penalties.length > 0) {
                html += `<div style="color:#ff4444; font-size:0.7rem; font-weight:bold; letter-spacing:1px; margin:10px 0 4px;">扣分項目</div>`;
                html += penalties.map(renderItem).join('');
            }
            html += `<div style="margin-top:10px; padding:8px 12px; background:rgba(79,172,254,0.08); border-radius:6px; border:1px solid rgba(79,172,254,0.2); display:flex; justify-content:space-between; align-items:center;">
                <span style="color:#888; font-size:0.78rem;">總分</span>
                <span style="color:#4facfe; font-size:1.1rem; font-weight:bold;">${score} 分</span>
            </div>`;
            breakdownEl.innerHTML = html;
        }
    }

    drawer.style.display = 'flex';
    overlay.style.display = 'block';
}

// ── Telegram 通知函數 ──────────────────────────────────────────────────────

// ── Telegram 多收件人設定 ────────────────────────────────────────────────────

let _tgRecipients = []; // 目前收件人快取

function toggleTgConfig() {
    const panel = document.getElementById('tg-config-panel');
    if (!panel) return;
    const isOpen = panel.style.display !== 'none';
    panel.style.display = isOpen ? 'none' : 'block';
    if (!isOpen) _loadTgConfig();
}

async function _loadTgConfig() {
    try {
        const res  = await fetch('/api/telegram/config');
        const data = await res.json();
        const tokenStatus = document.getElementById('tg-token-status');
        if (tokenStatus) tokenStatus.textContent = data.hasToken ? `✅ 已設定（${data.maskedToken}）` : '❌ 尚未設定';
        _tgRecipients = data.recipients || [];
        _renderRecipients();
    } catch(e) { console.error('讀取 TG 設定失敗', e); }
}

function _renderRecipients() {
    const list = document.getElementById('tg-recipients-list');
    if (!list) return;
    if (_tgRecipients.length === 0) {
        list.innerHTML = `<div style="color:#555;font-size:0.75rem;padding:6px 0;">尚未新增任何收件人</div>`;
        return;
    }
    list.innerHTML = _tgRecipients.map((r, i) => `
        <div style="display:flex;align-items:center;gap:8px;background:rgba(0,136,204,0.06);border:1px solid rgba(0,136,204,0.2);border-radius:6px;padding:6px 10px;">
            <span style="color:#29b6f6;font-size:0.8rem;font-weight:bold;min-width:70px;">👤 ${r.name}</span>
            <span style="color:#888;font-size:0.75rem;font-family:monospace;flex:1;">${r.chatId}</span>
            <button onclick="removeTgRecipient(${i})" style="width:auto;background:rgba(255,68,68,0.1);color:#ff4444;border:1px solid rgba(255,68,68,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.72rem;">✕ 移除</button>
        </div>
    `).join('');
}

function removeTgRecipient(idx) {
    _tgRecipients.splice(idx, 1);
    _renderRecipients();
    _saveTgRecipients();
}

async function addTgRecipient() {
    const name   = document.getElementById('tg-new-name')?.value.trim();
    const chatId = document.getElementById('tg-new-chatid')?.value.trim();
    const statusEl = document.getElementById('tg-config-status');
    if (!name || !chatId) {
        if (statusEl) statusEl.textContent = '❌ 名稱和 Chat ID 都必須填寫';
        return;
    }
    _tgRecipients.push({ name, chatId });
    _renderRecipients();
    document.getElementById('tg-new-name').value = '';
    document.getElementById('tg-new-chatid').value = '';
    await _saveTgRecipients();
    if (statusEl) statusEl.textContent = `✅ 已新增收件人：${name}`;
}

async function saveTgToken() {
    const token    = document.getElementById('tg-token-input')?.value.trim();
    const statusEl = document.getElementById('tg-config-status');
    if (!token) { if (statusEl) statusEl.textContent = '❌ Token 不可為空'; return; }
    if (statusEl) statusEl.textContent = '儲存中...';
    try {
        const res = await fetch('/api/telegram/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ botToken: token })
        });
        if (res.ok) {
            document.getElementById('tg-token-input').value = '';
            const tokenStatus = document.getElementById('tg-token-status');
            if (tokenStatus) tokenStatus.textContent = '✅ 已儲存';
            if (statusEl) statusEl.textContent = '✅ Bot Token 已儲存';
        } else {
            const err = await res.json();
            if (statusEl) statusEl.textContent = `❌ ${err.detail}`;
        }
    } catch(e) { if (statusEl) statusEl.textContent = `❌ ${e.message}`; }
}

async function _saveTgRecipients() {
    const statusEl = document.getElementById('tg-config-status');
    try {
        await fetch('/api/telegram/config', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ recipients: _tgRecipients })
        });
    } catch(e) { if (statusEl) statusEl.textContent = `❌ 儲存失敗：${e.message}`; }
}

async function _doTgSend(stocks, label, allStocks) {
    const btn = document.getElementById('tg-send-btn');
    const origText = btn ? btn.innerHTML : '';
    if (btn) { btn.innerHTML = '⏳ 傳送中...'; btn.disabled = true; }
    try {
        const res  = await fetch('/api/telegram/send', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ stocks, label, market_status: _marketStatus, all_stocks: allStocks || stocks })
        });
        const data = await res.json();
        if (res.ok) {
            _showTgToast(`✅ 已傳送 ${data.sent} 檔股票給 ${data.recipients} 位收件人！`);
        } else {
            _showTgToast(`❌ ${data.detail || '傳送失敗'}`, true);
        }
    } catch(e) {
        _showTgToast(`❌ 網路錯誤：${e.message}`, true);
    } finally {
        if (btn) { btn.innerHTML = origText; btn.disabled = false; }
    }
}

async function sendTelegramAlert() {
    const isSendable = i => i.industry !== 'ETF' && (i.buy_method || {}).allowed !== false;
    const byScore    = (a, b) => (b.score || 0) - (a.score || 0);

    const priority = _screenerAllData
        .filter(i => i.strategyState === '明日優先' && isSendable(i))
        .sort(byScore);

    let toSend = priority.slice(0, 5);

    if (toSend.length < 5) {
        const watching = _screenerAllData
            .filter(i => i.strategyState === '觀察中' && isSendable(i))
            .sort(byScore);
        toSend = [...toSend, ...watching.slice(0, 5 - toSend.length)];
    }

    if (toSend.length === 0) {
        _showTgToast('⚠️ 目前沒有符合條件的股票（請先執行篩選）', true);
        return;
    }
    await _doTgSend(toSend, '明日優先', _screenerAllData);
}

async function testTgSend() {
    const statusEl = document.getElementById('tg-config-status');
    if (statusEl) statusEl.textContent = '傳送測試訊息中...';
    try {
        const res = await fetch('/api/telegram/send', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                stocks: [{ stockCode: 'TEST', stockName: '測試股票', closePrice: 100, score: 88,
                           bias20: 3.5, return20: 12.3, entryPatternLabel: '突破前高',
                           stopLossPrice: 96, stopLossPercent: -4.0, institutionBuyRatio5: 8.5,
                           actionPlan: { trigger: '明日盤中突破今日高點', stopLoss: '跌破 96 停損', notes: '這是測試訊息' } }],
                label: '測試通知'
            })
        });
        const data = await res.json();
        if (res.ok) {
            if (statusEl) statusEl.textContent = `✅ 測試訊息已傳送給 ${data.recipients} 位收件人！`;
        } else {
            if (statusEl) statusEl.textContent = `❌ ${data.detail}`;
        }
    } catch(e) { if (statusEl) statusEl.textContent = `❌ ${e.message}`; }
}

async function triggerSchedulerNow() {
    const btn = event.currentTarget;
    const origText = btn.innerHTML;
    btn.innerHTML = '⏳ 執行中...';
    btn.disabled = true;
    try {
        const res = await fetch('/api/scheduler/trigger', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            _showTgToast('⏰ 排程已觸發！約 1 分鐘內完成同步+篩選+傳送 Telegram');
        } else {
            _showTgToast(`❌ ${data.detail || '觸發失敗'}`, true);
        }
    } catch(e) {
        _showTgToast(`❌ ${e.message}`, true);
    } finally {
        btn.innerHTML = origText;
        btn.disabled = false;
    }
}

function _showTgToast(msg, isError = false) {
    let toast = document.getElementById('tg-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'tg-toast';
        toast.style.cssText = 'position:fixed;bottom:30px;right:30px;z-index:99999;padding:12px 20px;border-radius:8px;font-size:0.85rem;font-weight:bold;box-shadow:0 4px 20px rgba(0,0,0,0.5);transition:opacity 0.4s;pointer-events:none;';
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.style.background = isError ? 'rgba(255,68,68,0.95)' : 'rgba(38,200,120,0.95)';
    toast.style.color = '#fff';
    toast.style.opacity = '1';
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toast.style.opacity = '0'; }, 3500);
}

// ── 整合選股 Telegram 管理 ───────────────────────────────────────────────────

function toggleIntegratedTgPanel() {
    const panel = document.getElementById('integrated-tg-panel');
    if (!panel) return;
    const isOpen = panel.style.display !== 'none';
    panel.style.display = isOpen ? 'none' : 'block';
    if (!isOpen) {
        _loadIntegratedTgTargets();
        _loadIntegratedTgPushStatus();
    }
}

async function _loadIntegratedTgPushStatus() {
    try {
        const res  = await fetch('/api/tg/push-status');
        const data = await res.json();
        const el   = document.getElementById('integrated-tg-push-status-block');
        if (!el) return;
        const statusColor = data.last_push_status === 'success' ? '#26de81' : data.last_push_status ? '#ff9f43' : '#555';
        const statusLabel = {
            success:         '✅ 成功',
            sync_failed:     '❌ 同步失敗',
            strategy_failed: '❌ 選股失敗',
            all_failed:      '❌ 傳送全部失敗',
            no_targets:      '⚠️ 無啟用目標',
        }[data.last_push_status] || (data.last_push_status ? data.last_push_status : '—');
        el.innerHTML = `
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 16px;color:#888;">
                <span>上次推送：<span style="color:#ccc;">${data.last_push_time || '—'}</span></span>
                <span>推送狀態：<span style="color:${statusColor};font-weight:bold;">${statusLabel}</span></span>
                <span>TG 精選數：<span style="color:#29b6f6;">${data.last_picks ?? '—'}</span></span>
                <span>TG 備選數：<span style="color:#29b6f6;">${data.last_watch ?? '—'}</span></span>
                <span>目標數量：<span style="color:#aaa;">${data.target_count ?? '—'}</span></span>
                <span>成功傳送：<span style="color:#26de81;">${data.sent_count ?? '—'}</span></span>
                ${data.last_error ? `<span style="color:#ff4444;grid-column:1/-1;">錯誤：${data.last_error}</span>` : ''}
            </div>`;
        // Update summary line
        const summary = document.getElementById('integrated-tg-status-summary');
        if (summary) {
            summary.textContent = data.last_push_time
                ? `上次推送：${data.last_push_time}｜精選 ${data.last_picks} 檔、備選 ${data.last_watch} 檔`
                : '尚未推送';
        }
    } catch(e) { console.error('TG push status 讀取失敗', e); }
}

async function _loadIntegratedTgTargets() {
    try {
        const res  = await fetch('/api/tg/targets');
        const data = await res.json();
        _renderIntegratedTgTargets(data.targets || []);
    } catch(e) { console.error('TG targets 讀取失敗', e); }
}

function _renderIntegratedTgTargets(targets) {
    const list = document.getElementById('integrated-tg-targets-list');
    if (!list) return;
    if (!targets || targets.length === 0) {
        list.innerHTML = `<div style="color:#555;font-size:0.75rem;padding:6px 0;">尚未新增任何目標</div>`;
        return;
    }
    const typeLabel = { stock: '股票', amplitude: '震幅統計', all: '全部' };
    const typeColor = { stock: '#ffd233', amplitude: '#4facfe', all: '#26de81' };
    list.innerHTML = targets.map(t => {
        const enabledColor = t.enabled ? '#26de81' : '#555';
        const enabledLabel = t.enabled ? '啟用中' : '已停用';
        const tt    = t.target_type || 'stock';
        const tLbl  = typeLabel[tt] || tt;
        const tClr  = typeColor[tt] || '#888';
        const nextTypes = { stock: 'amplitude', amplitude: 'all', all: 'stock' };
        const safeName   = (t.name || '').replace(/'/g, "\\'");
        const safeChatId = (t.chat_id || '').replace(/'/g, "\\'");
        return `<div style="display:flex;align-items:center;gap:8px;background:rgba(41,182,246,0.04);border:1px solid rgba(41,182,246,${t.enabled ? '0.2' : '0.08'});border-radius:6px;padding:6px 10px;">
            <span style="color:${enabledColor};font-size:0.72rem;min-width:48px;">${enabledLabel}</span>
            <span style="color:#29b6f6;font-size:0.8rem;font-weight:bold;min-width:80px;">👤 ${t.name || '未命名'}</span>
            <span style="color:#888;font-size:0.75rem;font-family:monospace;flex:1;">${t.chat_id}</span>
            <span title="推送類型（點擊切換）" onclick="changeIntegratedTgTargetType(${t.id}, '${nextTypes[tt]}')" style="color:${tClr};font-size:0.68rem;border:1px solid ${tClr};border-radius:4px;padding:1px 6px;cursor:pointer;white-space:nowrap;">${tLbl}</span>
            <button onclick="testSingleIntegratedTgTarget(${t.id})" title="單一測試" style="width:auto;background:rgba(255,211,51,0.1);color:#ffd233;border:1px solid rgba(255,211,51,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.7rem;">🧪</button>
            <button onclick="editIntegratedTgTarget(${t.id}, '${safeName}', '${safeChatId}')" title="編輯名稱與 Chat ID" style="width:auto;background:rgba(165,94,234,0.1);color:#e056fd;border:1px solid rgba(165,94,234,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.7rem;">✏️</button>
            <button onclick="toggleIntegratedTgTarget(${t.id}, ${t.enabled ? 0 : 1})" style="width:auto;background:rgba(41,182,246,0.1);color:#29b6f6;border:1px solid rgba(41,182,246,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.7rem;">${t.enabled ? '停用' : '啟用'}</button>
            <button onclick="deleteIntegratedTgTarget(${t.id})" style="width:auto;background:rgba(255,68,68,0.1);color:#ff4444;border:1px solid rgba(255,68,68,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.7rem;">✕ 刪除</button>
        </div>`;
    }).join('');
}

async function changeIntegratedTgTargetType(id, newType) {
    try {
        await fetch(`/api/tg/targets/${id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ target_type: newType }),
        });
        _loadIntegratedTgTargets();
    } catch(e) { _showTgToast(`❌ 更新類型失敗：${e.message}`, true); }
}

async function addIntegratedTgTarget() {
    const name       = document.getElementById('integrated-tg-new-name')?.value.trim();
    const chatId     = document.getElementById('integrated-tg-new-chatid')?.value.trim();
    const targetType = document.getElementById('integrated-tg-new-type')?.value || 'stock';
    const status = document.getElementById('integrated-tg-add-status');
    if (!chatId) {
        if (status) status.textContent = '❌ Chat ID 不可為空';
        return;
    }
    try {
        const res = await fetch('/api/tg/targets', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ chat_id: chatId, name: name || chatId, target_type: targetType }),
        });
        const data = await res.json();
        if (res.ok && data.success) {
            if (status) status.textContent = `✅ 已新增：${name || chatId}`;
            document.getElementById('integrated-tg-new-name').value   = '';
            document.getElementById('integrated-tg-new-chatid').value = '';
            _loadIntegratedTgTargets();
        } else {
            if (status) status.textContent = `❌ ${data.detail || '新增失敗'}`;
        }
    } catch(e) { if (status) status.textContent = `❌ ${e.message}`; }
}

async function toggleIntegratedTgTarget(id, enabled) {
    try {
        await fetch(`/api/tg/targets/${id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ enabled }),
        });
        _loadIntegratedTgTargets();
    } catch(e) { _showTgToast(`❌ 更新失敗：${e.message}`, true); }
}

async function deleteIntegratedTgTarget(id) {
    if (!confirm('確定要刪除此 Telegram 目標？')) return;
    try {
        await fetch(`/api/tg/targets/${id}`, { method: 'DELETE' });
        _loadIntegratedTgTargets();
    } catch(e) { _showTgToast(`❌ 刪除失敗：${e.message}`, true); }
}

async function editIntegratedTgTarget(id, currentName, currentChatId) {
    const newName = prompt('修改名稱：', currentName);
    if (newName === null) return;
    const newChatId = prompt('修改 Chat ID：', currentChatId);
    if (newChatId === null) return;
    if (!newChatId.trim()) {
        _showTgToast('❌ Chat ID 不可為空', true);
        return;
    }
    try {
        const res = await fetch(`/api/tg/targets/${id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ name: newName.trim(), chat_id: newChatId.trim() }),
        });
        const data = await res.json();
        if (res.ok && data.success) {
            _showTgToast('✅ 已更新名稱與 Chat ID');
            _loadIntegratedTgTargets();
        } else {
            _showTgToast(`❌ ${data.detail || '更新失敗'}`, true);
        }
    } catch(e) { _showTgToast(`❌ ${e.message}`, true); }
}

async function testSingleIntegratedTgTarget(id) {
    _showTgToast('📨 傳送測試訊息中...');
    try {
        const res  = await fetch(`/api/tg/test-send/${id}`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            _showTgToast('✅ 單一目標測試傳送成功！');
        } else {
            _showTgToast(`❌ ${data.detail || '傳送失敗'}`, true);
        }
    } catch(e) { _showTgToast(`❌ ${e.message}`, true); }
}

async function sendIntegratedTgTest() {
    const btn = document.getElementById('integrated-tg-test-btn');
    const origText = btn ? btn.innerHTML : '';
    if (btn) { btn.innerHTML = '⏳ 傳送中...'; btn.disabled = true; }
    try {
        const res  = await fetch('/api/tg/test-send', { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
            _showTgToast(`✅ 已傳送 — 精選 ${data.picks} 檔、備選 ${data.watch} 檔，傳送 ${data.sent} 個目標`);
        } else {
            _showTgToast(`❌ ${data.detail || '傳送失敗'}`, true);
        }
    } catch(e) {
        _showTgToast(`❌ 網路錯誤：${e.message}`, true);
    } finally {
        if (btn) { btn.innerHTML = origText; btn.disabled = false; }
    }
}

function closeStockDrawer() {
    const drawer = document.getElementById('stock-detail-drawer');
    const overlay = document.getElementById('drawer-overlay');
    if (drawer) drawer.style.display = 'none';
    if (overlay) overlay.style.display = 'none';
}

async function traceStockFilter() {
    const input    = document.getElementById('trace-code-input');
    const resultEl = document.getElementById('stock-trace-result');
    const queryBtn = document.getElementById('trace-query-btn');

    // 若元素找不到則 alert 提示方便除錯
    if (!input || !resultEl) {
        alert('追蹤面板元素未找到，請重新整理頁面後再試。');
        return;
    }

    const code = input.value.trim();
    if (!code) {
        resultEl.innerHTML = `<div style="color:#888;font-size:0.78rem;">請輸入股票代號後按查詢。</div>`;
        return;
    }

    resultEl.innerHTML = `<div style="color:#aaa;font-size:0.78rem;padding:8px 0;">⏳ 查詢 ${code} 中...</div>`;
    if (queryBtn) queryBtn.disabled = true;

    try {
        const res = await fetch('/api/screener/trace', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        const json = await res.json();
        if (json.status !== 'success') throw new Error(json.detail || '查詢失敗');
        const d = json.data;

        const chk = (v) => v
            ? `<span style="color:#26de81;font-weight:bold;">✓ 是</span>`
            : `<span style="color:#ff4444;font-weight:bold;">✗ 否</span>`;
        const fmtV = (v, pass) => {
            const color = pass ? '#26de81' : '#ff4444';
            const mark  = pass ? '✓' : '✗';
            return `<span style="color:${color};font-weight:bold;">${v > 0 ? '+' : ''}${v} 張 ${mark}</span>`;
        };

        const s1 = d.step1;
        const instDaysHtml = s1.instDays && s1.instDays.length > 0
            ? `<div style="margin-top:6px;background:rgba(255,255,255,0.03);border-radius:4px;padding:6px 8px;">
                <div style="color:#666;font-size:0.68rem;margin-bottom:4px;">近期法人每日明細</div>
                <table style="width:100%;border-collapse:collapse;font-size:0.72rem;font-family:monospace;">
                  <tr style="color:#555;"><td style="padding:1px 6px;">日期</td><td style="padding:1px 6px;text-align:right;">外資</td><td style="padding:1px 6px;text-align:right;">投信</td><td style="padding:1px 6px;text-align:right;">自營</td></tr>
                  ${s1.instDays.map(r => `<tr>
                    <td style="padding:1px 6px;color:#888;">${r.date}</td>
                    <td style="padding:1px 6px;text-align:right;color:${r.foreign>0?'#ff4444':r.foreign<0?'#44ff44':'#666'};">${r.foreign>0?'+':''}${r.foreign}</td>
                    <td style="padding:1px 6px;text-align:right;color:${r.invest>0?'#ff4444':r.invest<0?'#44ff44':'#666'};">${r.invest>0?'+':''}${r.invest}</td>
                    <td style="padding:1px 6px;text-align:right;color:${r.dealer>0?'#ff4444':r.dealer<0?'#44ff44':'#666'};">${r.dealer>0?'+':''}${r.dealer}</td>
                  </tr>`).join('')}
                </table>
               </div>`
            : '';

        const msgHtml = d.messages && d.messages.length > 0
            ? d.messages.map(m => `<div style="background:rgba(255,211,51,0.08);border-left:3px solid #ffd233;border-radius:4px;padding:6px 10px;margin-top:6px;color:#ffd233;font-size:0.76rem;">⚠️ ${m}</div>`).join('')
            : '';

        const s2 = d.step2Liquidity || {};
        const s3 = d.step3Technical || {};
        const s4 = d.step4Strength  || {};

        const stepSection = (title, color, passed, bodyHtml) => `
            <div style="background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:6px;padding:8px 10px;margin-bottom:6px;">
                <div style="color:${color};font-size:0.7rem;font-weight:bold;margin-bottom:5px;">${title}
                    <span style="margin-left:8px;font-weight:normal;">${passed === true ? '<span style="color:#26de81;">✓ 通過</span>' : passed === false ? '<span style="color:#ff4444;">✗ 未通過</span>' : ''}</span>
                </div>
                ${bodyHtml}
            </div>`;

        const rowPair = (label, value) => `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:3px 6px;background:rgba(255,255,255,0.02);border-radius:3px;font-size:0.76rem;">
                <span style="color:#aaa;">${label}</span><span style="color:#ccc;font-family:monospace;">${value}</span>
            </div>`;

        const instRatioVal = d.institutionBuyRatio5 !== undefined ? d.institutionBuyRatio5 : (d.inst_ratio_5d || 0);
        const hasInstWarning = d.highInstRatioWarning || instRatioVal > 30;

        const instWarningHtml = hasInstWarning
            ? `<div style="background:rgba(255,211,51,0.1);border-left:3px solid #ffd233;border-radius:4px;padding:6px 10px;margin-top:6px;color:#ffd233;font-size:0.74rem;">⚠️ 法人佔比偏高，請確認成交金額與流動性<br><span style="color:#888;font-size:0.7rem;">法人佔比過高可能代表籌碼集中，也可能是成交金額較小導致比例被放大。</span></div>`
            : '';

        const sStyle = d.strategyState === '明日優先' ? '#26de81' : d.strategyState === '突破觀察' ? '#4facfe' : d.strategyState === '等回測' ? '#ffd233' : d.strategyState === '過熱警戒' ? '#ff4444' : '#888';
        const decisionHtml = d.strategyState ? stepSection('決策結果', '#ff9f43', null, `
            <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;font-size:0.78rem;">
                <span style="background:rgba(255,159,67,0.15);border:1px solid rgba(255,159,67,0.3);padding:2px 10px;border-radius:12px;color:#ff9f43;">總分 ${d.totalScore || 0}</span>
                <span style="background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);padding:2px 10px;border-radius:12px;color:${sStyle};">${d.strategyStateLabel || d.strategyState}</span>
                <span style="background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);padding:2px 10px;border-radius:12px;color:#aaa;">${d.entryPatternLabel || d.entryPattern || '--'}</span>
            </div>
            ${rowPair('法人佔比5日', `${instRatioVal.toFixed ? instRatioVal.toFixed(2) : instRatioVal}%`)}
            ${rowPair('高法人佔比警示', hasInstWarning ? '<span style="color:#ffd233;font-weight:bold;">⚠️ 是</span>' : '<span style="color:#555;">否</span>')}
            ${rowPair('流動性通過', d.finalIncluded !== undefined ? (d.step2Liquidity && d.step2Liquidity.passed ? '<span style="color:#26de81;">✓ 是</span>' : '<span style="color:#ff4444;">✗ 否</span>') : '--')}
            ${rowPair('最終納入候選', d.finalIncluded ? '<span style="color:#26de81;font-weight:bold;">✓ 是</span>' : '<span style="color:#ff4444;font-weight:bold;">✗ 否</span>')}
            ${d.excludedAtStep ? rowPair('排除階段', `<span style="color:#ff7777;">${d.excludedAtStep}: ${d.excludedReason}</span>`) : ''}
            ${instWarningHtml}
        `) : '';

        resultEl.innerHTML = `
            <div style="border-top:1px solid rgba(38,222,129,0.15);padding-top:8px;margin-top:4px;">
                <div style="color:#fff;font-weight:bold;font-size:0.9rem;margin-bottom:8px;">
                    <span style="color:#4facfe;font-family:monospace;">${d.code}</span>
                    <span style="margin-left:6px;">${d.name || '--'}</span>
                </div>

                <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:8px;font-size:0.78rem;">
                    <div style="background:rgba(255,255,255,0.03);padding:5px 8px;border-radius:4px;">
                        <span style="color:#666;">在選股名單中</span><br>${chk(d.inUniverse)}
                    </div>
                    <div style="background:rgba(255,255,255,0.03);padding:5px 8px;border-radius:4px;">
                        <span style="color:#666;">有 daily_kbars</span><br>
                        ${chk(d.hasKbars)}
                        ${d.kbarCount > 0 ? `<span style="color:#555;font-size:0.68rem;margin-left:4px;">${d.kbarCount}根 / 最新${d.latestKbarDate||'--'}</span>` : ''}
                    </div>
                </div>

                ${stepSection('Step 1｜法人篩選（近5日）', '#4facfe', s1.passed, `
                    <div style="display:flex;flex-direction:column;gap:3px;font-size:0.76rem;">
                        <div style="display:flex;justify-content:space-between;align-items:center;padding:3px 6px;background:rgba(255,255,255,0.02);border-radius:3px;">
                            <span style="color:#aaa;">外資近5日買超</span>${fmtV(s1.foreignBuy5, s1.hasForeignBuy)}
                        </div>
                        <div style="display:flex;justify-content:space-between;align-items:center;padding:3px 6px;background:rgba(255,255,255,0.02);border-radius:3px;">
                            <span style="color:#aaa;">投信近5日買超</span>${fmtV(s1.investmentBuy5, s1.hasInvestmentBuy)}
                        </div>
                        <div style="display:flex;justify-content:space-between;align-items:center;padding:3px 6px;background:rgba(255,255,255,0.02);border-radius:3px;">
                            <span style="color:#aaa;">三大法人合計</span>${fmtV(s1.totalBuy5, s1.hasTotalBuy)}
                        </div>
                        <div style="display:flex;justify-content:space-between;padding:3px 6px;background:rgba(255,255,255,0.04);border-radius:3px;margin-top:2px;font-size:0.75rem;">
                            <span style="color:#aaa;">法人分數 / 標籤</span>
                            <span><span style="color:${(s1.instScore||0)>=15?'#ffd233':'#4facfe'};font-weight:bold;">${s1.instScore||0}分</span> <span style="color:${s1.instLabel==='三人同買'?'#26de81':s1.instLabel==='外資主導'?'#4facfe':s1.instLabel==='投信主導'?'#fd9644':'#555'};">${s1.instLabel||'--'}</span></span>
                        </div>
                    </div>
                    ${!s1.passed ? `<div style="margin-top:4px;background:rgba(255,68,68,0.08);border-left:3px solid #ff4444;border-radius:3px;padding:4px 8px;color:#ff7777;font-size:0.68rem;">原因：${s1.reason}</div>` : ''}
                    ${instDaysHtml}
                `)}

                ${s2.threshold !== undefined ? stepSection('Step 2｜流動性', '#26de81', s2.passed, `
                    <div style="display:flex;flex-direction:column;gap:3px;font-size:0.76rem;">
                        ${rowPair('成交金額5日均', `${((s2.amountMa5||0)/1e8).toFixed(2)} 億`)}
                        ${rowPair('成交金額20日均', `${((s2.amountMa20||0)/1e8).toFixed(2)} 億`)}
                        ${rowPair('門檻', `${((s2.threshold||0)/1e6).toFixed(0)} 萬`)}
                        ${s2.reason ? `<div style="color:${s2.passed?'#26de81':'#ff7777'};font-size:0.7rem;padding:3px 6px;margin-top:2px;">${s2.reason}</div>` : ''}
                    </div>
                `) : ''}

                ${s3.close !== null && s3.close !== undefined ? stepSection('Step 3｜技術條件（多頭排列）', '#fd9644', s3.trendPassed, `
                    <div style="display:flex;flex-direction:column;gap:3px;font-size:0.76rem;">
                        ${rowPair('收盤價', s3.close !== undefined ? s3.close.toFixed(2) : '--')}
                        ${rowPair('20日均線', s3.ma20 !== undefined ? s3.ma20.toFixed(2) : '--')}
                        ${rowPair('60日均線', s3.ma60 !== undefined ? s3.ma60.toFixed(2) : '--')}
                        ${s3.reason ? `<div style="color:${s3.trendPassed?'#26de81':'#ff7777'};font-size:0.7rem;padding:3px 6px;margin-top:2px;">${s3.reason}</div>` : ''}
                    </div>
                `) : ''}

                ${s4.return20 !== null && s4.return20 !== undefined ? stepSection('Step 4｜相對強度', '#a29bfe', s4.return20Passed, `
                    <div style="display:flex;flex-direction:column;gap:3px;font-size:0.76rem;">
                        ${rowPair('20日漲幅', `${s4.return20>=0?'+':''}${s4.return20}%（門檻 >${s4.return20Threshold}%，${s4.return20Passed?'<span style="color:#26de81;">通過</span>':'<span style="color:#ff4444;">未通過</span>'}）`)}
                        ${rowPair('60日漲幅', `${s4.return60>=0?'+':''}${s4.return60}%（${s4.return60Passed?'<span style="color:#26de81;">+10分</span>':'<span style="color:#888;">不加分，不排除</span>'}）`)}
                    </div>
                `) : ''}

                ${decisionHtml}

                ${msgHtml}
            </div>`;
    } catch (e) {
        resultEl.innerHTML = `<div style="color:#ff4444;font-size:0.78rem;">❌ 查詢失敗：${e.message}</div>`;
    } finally {
        if (queryBtn) queryBtn.disabled = false;
    }
}

async function runScreener() {
    const resultsBody = document.getElementById('screener-results-body');
    const runBtn = document.getElementById('run-screener-btn');

    if (!resultsBody) return;

    resultsBody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:#aaa;padding:50px;"><div class="loading-spinner" style="border:3px solid rgba(255,159,67,0.1);border-top:3px solid #ff9f43;border-radius:50%;width:30px;height:30px;animation:spin 1s linear infinite;margin:0 auto 10px auto;"></div><div style="font-size:0.9rem;color:#ff9f43;">正在分析台股主力突破標的，請稍候...</div></td></tr>`;

    if (runBtn) { runBtn.disabled = true; runBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> 正在篩選...`; }

    try {
        const res = await fetch('/api/screener/run', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ max_decline_pct: -3.5 })
        });
        const data = await res.json();

        if (data.status === 'success' && Array.isArray(data.data)) {
            _screenerAllData = data.data;
            _marketStatus    = data.market_status || null;
            _renderMarketStatusCard(_marketStatus);
            _updateStrategyTabs(_screenerAllData);
            _applyStrategyTab(_activeStrategyTab);
        } else {
            _screenerAllData = [];
            _renderMarketStatusCard(null);
            resultsBody.innerHTML = `<tr><td colspan="14" style="text-align:center;color:#ff4444;padding:40px;">❌ 篩選失敗，請稍後再試。</td></tr>`;
        }
    } catch (e) {
        console.error("執行選股篩選失敗:", e);
        resultsBody.innerHTML = `<tr><td colspan="12" style="text-align:center;color:#ff4444;padding:40px;">❌ 選股計算發生異常：<br>${e.message}</td></tr>`;
    } finally {
        if (runBtn) { runBtn.disabled = false; runBtn.innerHTML = `🔍 執行策略篩選`; }
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
    
    const tabRankingBtn    = document.getElementById('tab-ranking-btn');
    const tabScreenerBtn   = document.getElementById('tab-screener-btn');
    const tabIndustryBtn   = document.getElementById('tab-industry-btn');
    const tabTomorrowBtn   = document.getElementById('tab-tomorrow-btn');
    const tabIntegratedBtn = document.getElementById('tab-integrated-btn');
    const tabBrokerBtn     = document.getElementById('tab-broker-btn');

    let activeStockTab = 'integrated'; // 預設股票模式時顯示「整合選股」

    const applyMarket = (market) => {
        const rankingsView   = document.getElementById('stock-rankings-view');
        const screenerView   = document.getElementById('stock-screener-view');
        const industryView   = document.getElementById('stock-industry-view');
        const tomorrowView   = document.getElementById('stock-tomorrow-view');
        const integratedView = document.getElementById('stock-integrated-view');
        const brokerView     = document.getElementById('stock-broker-view');
        const freelancerContainer = document.getElementById('freelancer-container');
        const ampStatsContainer   = document.getElementById('amplitude-statistics-container');
        const panesEl             = document.getElementById('panes-container');

        // ── 全域 reset：清除所有模式 class，隱藏所有可切換容器 ──
        appContainer.classList.remove('market-stocks', 'market-freelancer', 'market-amplitude-statistics');
        if (freelancerContainer) freelancerContainer.style.display = 'none';
        if (ampStatsContainer)   ampStatsContainer.style.display   = 'none';
        // 移除震幅統計模式注入的動態 style 覆蓋（讓 CSS 重新接管 panes-container）
        const _existOverride = document.getElementById('_amp-panes-override');
        if (_existOverride) _existOverride.remove();
        // 停止震幅統計自動更新
        stopAmplitudeStatisticsAutoRefresh();

        if (market === 'stocks') {
            appContainer.classList.add('market-stocks');
            if (tabsBar) tabsBar.style.display = 'flex';
            if (placeholder) placeholder.style.display = 'flex';

            // 重設所有 tab 樣式
            [tabRankingBtn, tabScreenerBtn, tabIndustryBtn, tabTomorrowBtn, tabIntegratedBtn, tabBrokerBtn].forEach(b => {
                if (b) { b.style.color = '#888'; b.style.borderBottomColor = 'transparent'; }
            });
            // 隱藏所有面板
            if (rankingsView)   rankingsView.style.display   = 'none';
            if (screenerView)   screenerView.style.display   = 'none';
            if (industryView)   industryView.style.display   = 'none';
            if (tomorrowView)   tomorrowView.style.display   = 'none';
            if (integratedView) integratedView.style.display = 'none';
            if (brokerView) brokerView.style.display = 'none';

            // 啟用目前 tab
            if (activeStockTab === 'ranking') {
                if (tabRankingBtn) { tabRankingBtn.style.color = '#ff9f43'; tabRankingBtn.style.borderBottomColor = '#ff9f43'; }
                if (rankingsView) rankingsView.style.display = 'flex';
                loadInstitutionalRankings();
            } else if (activeStockTab === 'screener') {
                if (tabScreenerBtn) { tabScreenerBtn.style.color = '#ff9f43'; tabScreenerBtn.style.borderBottomColor = '#ff9f43'; }
                if (screenerView) screenerView.style.display = 'flex';
                loadInstitutionalRankings();
            } else if (activeStockTab === 'tomorrow') {
                if (tabTomorrowBtn) { tabTomorrowBtn.style.color = '#ff9f43'; tabTomorrowBtn.style.borderBottomColor = '#ff9f43'; }
                if (tomorrowView) tomorrowView.style.display = 'flex';
                loadTomorrowStrategy();
            } else if (activeStockTab === 'integrated') {
                if (tabIntegratedBtn) { tabIntegratedBtn.style.color = '#26de81'; tabIntegratedBtn.style.borderBottomColor = '#26de81'; }
                if (integratedView) integratedView.style.display = 'flex';
                loadIntegratedStrategy();
                _loadIntegratedTgTargets();
                _loadIntegratedTgPushStatus();
            } else if (activeStockTab === 'broker') {
                if (tabBrokerBtn) { tabBrokerBtn.style.color = '#26de81'; tabBrokerBtn.style.borderBottomColor = '#26de81'; }
                if (brokerView) brokerView.style.display = 'flex';
            } else { // industry
                if (tabIndustryBtn) { tabIndustryBtn.style.color = '#ff9f43'; tabIndustryBtn.style.borderBottomColor = '#ff9f43'; }
                if (industryView) industryView.style.display = 'flex';
                loadIndustryRankings();
            }
        } else if (market === 'freelancer') {
            appContainer.classList.add('market-freelancer');
            if (freelancerContainer) freelancerContainer.style.display = 'flex';
            // stock tabs/placeholder 由 CSS market-freelancer class 隱藏
            setTimeout(() => ensureFreelancerChart(contractCode || 'TXFR1'), 0);
            flStartAmplitudeRefresh();
        } else if (market === 'amplitude-statistics') {
            // ── 震幅統計：動態注入 <style> 來隱藏 panes-container ──
            // CSS style.css 有 `#panes-container { display: grid !important }`，
            // 任何 inline style（含 setProperty !important）都無法可靠覆蓋它。
            // 動態插入的 <style> 在 head 末端，source order 最晚，同 specificity 下必勝。
            let _override = document.getElementById('_amp-panes-override');
            if (!_override) {
                _override = document.createElement('style');
                _override.id = '_amp-panes-override';
                document.head.appendChild(_override);
            }
            _override.textContent = '#panes-container { display: none !important; }';
            if (tabsBar)     tabsBar.style.display     = 'none';
            if (placeholder) placeholder.style.display = 'none';
            appContainer.classList.add('market-amplitude-statistics');
            if (ampStatsContainer) ampStatsContainer.style.display = 'flex';
            loadAmplitudeStatistics();
            loadAmplitudeTgTargets();
        } else { // futures
            flStopAmplitudeRefresh();
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
        
        // 初始載入時，生成對應市場的選單項目（自由人/震幅統計模式不需要合約選單）
        if (savedMarket !== 'freelancer' && savedMarket !== 'amplitude-statistics') updateContractSelector(savedMarket, contractCode);
        
        marketSelector.value = savedMarket;
        applyMarket(savedMarket);
        
        marketSelector.addEventListener('change', (e) => {
            const market = e.target.value;
            localStorage.setItem('global-market-type', market);
            
            // 當切換市場時，自動載入該市場之預設合約
            const defaultCode = (market === 'futures') ? 'TXFR1' : (market === 'stocks') ? '2330' : null;
            if (defaultCode) updateContractSelector(market, defaultCode);
            
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
    if (tabRankingBtn)    { tabRankingBtn.onclick    = () => { activeStockTab = 'ranking';    applyMarket('stocks'); }; }
    if (tabScreenerBtn)   { tabScreenerBtn.onclick   = () => { activeStockTab = 'screener';   applyMarket('stocks'); }; }
    if (tabIndustryBtn)   { tabIndustryBtn.onclick   = () => { activeStockTab = 'industry';   applyMarket('stocks'); }; }
    if (tabTomorrowBtn)   { tabTomorrowBtn.onclick   = () => { activeStockTab = 'tomorrow';   applyMarket('stocks'); }; }
    if (tabIntegratedBtn) { tabIntegratedBtn.onclick = () => { activeStockTab = 'integrated'; applyMarket('stocks'); }; }
    if (tabBrokerBtn)     { tabBrokerBtn.onclick     = () => { activeStockTab = 'broker';     applyMarket('stocks'); initBrokerTabSearch(); }; }
    initBrokerTabSearch();
    
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
        runScreenerBtn.onclick = () => { runScreener(); };
    }

    // 0.3 綁定策略狀態 Tab 篩選按鈕
    document.querySelectorAll('.strategy-tab-btn').forEach(btn => {
        btn.addEventListener('click', () => { _applyStrategyTab(btn.dataset.state); });
    });

    // 0.4 綁定 Drawer 關閉按鈕
    const drawerCloseBtn = document.getElementById('drawer-close-btn');
    if (drawerCloseBtn) drawerCloseBtn.onclick = closeStockDrawer;
    
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
                    // 同步後刷新明日策略
                    await loadTomorrowStrategy();
                    // 同步後刷新整合選股
                    await loadIntegratedStrategy();
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
                    if (freelancerChartPane) freelancerChartPane.symbol = code;

                    // 執行重新載入
                    const reloads = panes.map(p => p.reload());
                    if (freelancerChartPane) reloads.push(freelancerChartPane.reload());
                    await Promise.all(reloads);
                    
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

    // 5.5 載入快取資訊徽章，之後每分鐘刷新
    loadCacheInfo();
    setInterval(loadCacheInfo, 60000);

    // 6. 啟動定時補漏 (每 30 秒一次)
    setInterval(async () => {
        // 🎯 非期貨模式安全哨兵：股票或自由人模式下，不進行期貨快照輪詢
        const mSelector = document.getElementById('market-type-selector');
        if (mSelector && mSelector.value !== 'futures') return;
        
        try {
            const sRes = await fetch('/api/snapshot');
            const snap = await sRes.json();
            if (snap) updateFullUI(snap);
        } catch (e) {}
    }, 30000);

    // 7. 啟動秒級心跳 (僅針對「非連動中」且「非滑鼠指著」的視窗更新倒數)
    setInterval(() => {
        // 🎯 非期貨模式安全哨兵：股票或自由人模式下，不更新圖表心跳
        const mSelector = document.getElementById('market-type-selector');
        if (mSelector && mSelector.value !== 'futures') return;
        
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
    // 🎯 非期貨模式安全哨兵：股票或自由人模式下，不更新任何期貨欄位，維持乾淨
    const mSelector = document.getElementById('market-type-selector');
    if (mSelector && mSelector.value !== 'futures') return;
    
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
            is_simulation: false,
            ca_path: '',
            ca_passwd: '',
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

// ── 歷史快取狀態徽章 ────────────────────────────────────────────────
let _cachePopoverOpen = false;

function toggleCacheInfoPopover(e) {
    e.stopPropagation();
    _cachePopoverOpen = !_cachePopoverOpen;
    const pop = document.getElementById('cache-info-popover');
    if (pop) pop.style.display = _cachePopoverOpen ? 'block' : 'none';
}

document.addEventListener('click', () => {
    if (_cachePopoverOpen) {
        _cachePopoverOpen = false;
        const pop = document.getElementById('cache-info-popover');
        if (pop) pop.style.display = 'none';
    }
});

async function loadCacheInfo() {
    try {
        const res = await fetch('/api/cache_info');
        if (!res.ok) return;
        const d = await res.json();
        const badge = document.getElementById('cache-info-badge');
        const text  = document.getElementById('cache-info-text');
        if (!badge || !text || d.count === 0) return;

        const f = d.first.slice(5);  // MM-DD
        const l = d.last.slice(5);
        const rtSuffix = d.rt_bars_today > 0 ? ` +今日${d.rt_bars_today}筆` : '';
        text.textContent = `${f}~${l} (${d.count}日)${rtSuffix}`;
        badge.style.display = 'flex';

        // 彈出詳細內容
        const detail = document.getElementById('cache-info-detail');
        if (!detail) return;
        const rtLine = d.rt_bars_today > 0
            ? `<div style="color:#00ff88;margin-top:8px;padding-top:8px;border-top:1px solid #333">🔴 今日即時已存 ${d.rt_bars_today} 筆 1min bar<br><span style="color:#666;font-size:0.72rem">（圖表重新載入即可顯示）</span></div>`
            : '';

        // 按月份分組
        const byMonth = {};
        d.dates.forEach(dt => {
            const m = dt.slice(0, 7);
            (byMonth[m] = byMonth[m] || []).push(dt.slice(8));
        });
        const dateRows = Object.entries(byMonth).sort().map(([m, days]) =>
            `<div style="margin-top:5px"><span style="color:#4facfe">${m}</span>: ${days.join(', ')}</div>`
        ).join('');

        detail.innerHTML = `
            <div style="font-weight:bold;color:#4facfe;margin-bottom:8px">💾 歷史 K 棒快取</div>
            <div>區間：${d.first} ~ ${d.last}</div>
            <div>交易日：${d.count} 天</div>
            ${rtLine}
            <div style="margin-top:8px;border-top:1px solid #333;padding-top:8px;font-size:0.72rem;color:#888;line-height:1.8">${dateRows}</div>
        `;
    } catch(e) { /* silent */ }
}
// ────────────────────────────────────────────────────────────────────

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
        const msg = JSON.parse(event.data);
        if (msg.type === 'cache_updated') {
            loadCacheInfo();
            return;
        }

        // 🎯 自由人模式只把 Tick 餵給自由人 K 線主圖，不更新期貨看盤頁的報價 UI
        const mSelector = document.getElementById('market-type-selector');
        if (mSelector && mSelector.value === 'freelancer') {
            if (msg.type === 'tick' && freelancerChartPane) {
                freelancerChartPane.onTick(msg.data.price, msg.data.time);
            }
            return;
        }

        // 🎯 非期貨模式安全哨兵：股票模式下，不接收或解析期貨即時 Tick 行情，維持靜默
        if (mSelector && mSelector.value !== 'futures') return;

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
        if (freelancerChartPane) freelancerChartPane.resize();
    }, 100);
});

// 監聽視窗焦點：當使用者從別的分頁回來時，立刻刷最新數據補洞
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        console.log("[App] Tab active. Refreshing data to fill gaps...");
        if (panes && panes.length > 0) {
            panes.forEach(p => p.reload());
        }
        if (freelancerChartPane) freelancerChartPane.reload();
    }
});

checkStatus();

// ═══════════════════════════════════════════════════════════════
//  產業排行 — 資料載入 + 渲染
// ═══════════════════════════════════════════════════════════════

let _industryData      = [];   // 完整原始資料
let _selectedIndustry  = null; // 目前點選的產業名稱
let _industryFilter    = 'all';
let _industrySort      = 'score';

async function loadIndustryRankings() {
    const body     = document.getElementById('industry-body');
    const loading  = document.getElementById('industry-loading');
    const emptyTip = document.getElementById('industry-empty-tip');
    if (!body) return;
    body.style.display    = 'none';
    emptyTip.style.display = 'none';
    loading.style.display  = 'block';

    try {
        const res  = await fetch('/api/industry_rankings');
        const json = await res.json();
        if (!json.data || json.data.length === 0) {
            loading.style.display  = 'none';
            emptyTip.style.display = 'block';
            return;
        }
        _industryData = json.data;
        loading.style.display = 'none';
        body.style.display    = 'flex';
        _renderIndustrySummaryCards(_industryData);
        _renderHeatmap();
        // 預設選第一個產業
        if (_industryData.length > 0) {
            _selectIndustry(_industryData[0].industryName);
        }
    } catch (e) {
        loading.style.display  = 'none';
        emptyTip.style.display = 'block';
        console.error('loadIndustryRankings error', e);
    }
}

// ── 顏色工具 ─────────────────────────────────────────────────
function _industryScoreColor(score) {
    if (score >= 90) return '#FF4D3D';
    if (score >= 80) return '#FF8A00';
    if (score >= 70) return '#D6A329';
    if (score >= 60) return '#4a4a4a';
    return '#263645';
}
function _industryStatusBorderColor(status) {
    const map = { '強勢主流':'#FF4D3D','健康偏強':'#FF8A00','回測機會':'#00E676',
                  '突破集中':'#2196F3','過熱警戒':'#B83280','中性觀察':'#444','弱勢產業':'#263645' };
    return map[status] || '#333';
}
function _strategyStateColor(s) {
    const map = { '明日優先':'#00C853','突破觀察':'#2196F3','等回測':'#C9B400',
                  '過熱警戒':'#B3261E','暫不交易':'#555' };
    return map[s] || '#888';
}

// ── Summary Cards ─────────────────────────────────────────────
function _renderIndustrySummaryCards(data) {
    const el = document.getElementById('industry-summary-cards');
    if (!el) return;

    const topScore   = [...data].sort((a,b) => b.industryScore - a.industryScore)[0];
    const topInst    = [...data].sort((a,b) => b.avgInstRatio - a.avgInstRatio)[0];
    const topBreak   = [...data].sort((a,b) => b.breakoutCount - a.breakoutCount)[0];
    const topHot     = [...data].sort((a,b) => b.overheatCount - a.overheatCount)[0];

    const card = (icon, label, name, sub, color) => `
        <div style="background:#111; border:1px solid #222; border-radius:10px; padding:12px 14px; border-top:3px solid ${color}; cursor:pointer;" onclick="_selectIndustry('${name}')">
            <div style="font-size:0.7rem; color:#666; margin-bottom:4px;">${icon} ${label}</div>
            <div style="font-size:0.9rem; font-weight:bold; color:#fff; margin-bottom:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;">${name}</div>
            <div style="font-size:0.75rem; color:${color};">${sub}</div>
        </div>`;

    el.innerHTML =
        card('🔥','最強產業',   topScore?.industryName  || '--', `產業分數 ${topScore?.industryScore  || 0}`, '#FF4D3D') +
        card('💰','法人最集中', topInst?.industryName   || '--', `法人佔比 ${topInst?.avgInstRatio    || 0}%`, '#FFD54F') +
        card('🚀','突破最多',   topBreak?.industryName  || '--', `突破股 ${topBreak?.breakoutCount    || 0} 檔`, '#2196F3') +
        card('⚠️','過熱警戒',  topHot?.industryName    || '--', `過熱股 ${topHot?.overheatCount      || 0} 檔`, '#B83280');
}

// ── 熱力圖 ───────────────────────────────────────────────────
function _getFilteredSorted() {
    let list = [..._industryData];
    // 篩選
    if (_industryFilter === 'strong')  list = list.filter(d => d.industryScore >= 80);
    else if (_industryFilter === 'inst')   list = list.filter(d => d.avgInstRatio >= (_industryData.reduce((s,x)=>s+x.avgInstRatio,0)/_industryData.length));
    else if (_industryFilter === 'break')  list = list.filter(d => d.breakoutCount >= 2);
    else if (_industryFilter === 'pull')   list = list.filter(d => d.pullbackCount >= 1);
    else if (_industryFilter === 'hot')    list = list.filter(d => d.overheatRatio >= 0.30);
    // 排序
    const sortMap = {
        score:    (a,b) => b.industryScore - a.industryScore,
        count:    (a,b) => b.candidateCount - a.candidateCount,
        r20:      (a,b) => b.avgReturn20 - a.avgReturn20,
        r60:      (a,b) => b.avgReturn60 - a.avgReturn60,
        inst:     (a,b) => b.avgInstRatio - a.avgInstRatio,
        breakout: (a,b) => b.breakoutRatio - a.breakoutRatio,
        overheat: (a,b) => b.overheatRatio - a.overheatRatio,
    };
    list.sort(sortMap[_industrySort] || sortMap.score);
    return list;
}

function _renderHeatmap() {
    const el = document.getElementById('industry-heatmap');
    if (!el) return;
    const list = _getFilteredSorted();
    if (list.length === 0) {
        el.innerHTML = '<div style="color:#555; font-size:0.82rem; padding:16px;">此篩選條件下沒有符合的產業</div>';
        return;
    }
    const maxCandidates = Math.max(...list.map(d => d.candidateCount), 1);
    el.innerHTML = list.map(d => {
        const baseSize  = 90;
        const tileSize  = Math.round(baseSize + (d.candidateCount / maxCandidates) * 80);
        const bgColor   = _industryScoreColor(d.industryScore);
        const border    = _industryStatusBorderColor(d.status);
        const isActive  = d.industryName === _selectedIndustry;
        const priorityTag = d.priorityCount > 0 ? `<div style="font-size:0.62rem; color:#00C853; margin-top:2px;">🟢 優先${d.priorityCount}檔</div>` : '';
        return `
        <div class="ind-tile" data-name="${d.industryName}"
            style="width:${tileSize}px; height:${tileSize}px; background:${bgColor}22;
                   border:2px solid ${isActive ? '#ff9f43' : border};
                   border-radius:10px; padding:8px; cursor:pointer; box-sizing:border-box;
                   display:flex; flex-direction:column; justify-content:space-between;
                   transition:border-color 0.15s, transform 0.15s;
                   ${isActive ? 'transform:scale(1.04); box-shadow:0 0 12px rgba(255,159,67,0.4);' : ''}
                   overflow:hidden;"
            onclick="_selectIndustry('${d.industryName}')">
            <div>
                <div style="font-size:0.72rem; font-weight:bold; color:#fff; line-height:1.3; word-break:break-all;">${d.industryName}</div>
                <div style="font-size:0.65rem; color:${bgColor === '#263645' ? '#4a7fa0' : '#ffcb8b'}; margin-top:2px;">分數 ${d.industryScore}</div>
            </div>
            <div>
                <div style="font-size:0.62rem; color:#aaa;">候選 ${d.candidateCount} 檔</div>
                <div style="font-size:0.62rem; color:#888;">20D ${d.avgReturn20 >= 0 ? '+' : ''}${d.avgReturn20}%</div>
                ${priorityTag}
            </div>
        </div>`;
    }).join('');
}

// ── 點擊產業 → 個股清單 ──────────────────────────────────────
function _selectIndustry(name) {
    _selectedIndustry = name;
    // 重繪熱力圖（更新 active 樣式）
    _renderHeatmap();

    const d = _industryData.find(x => x.industryName === name);
    const panel  = document.getElementById('industry-stock-panel');
    const header = document.getElementById('industry-stock-header');
    const tbody  = document.getElementById('industry-stock-tbody');
    if (!d || !panel) return;

    panel.style.display = 'block';

    const statusColor = _industryStatusBorderColor(d.status);
    header.innerHTML = `
        <span style="font-weight:bold; color:#fff; font-size:0.9rem;">${d.industryName}</span>
        <span style="background:${statusColor}22; border:1px solid ${statusColor}; color:${statusColor}; font-size:0.7rem; padding:2px 8px; border-radius:10px;">${d.status}</span>
        <span style="color:#aaa;">產業分數 <strong style="color:#ff9f43;">${d.industryScore}</strong></span>
        <span style="color:#aaa;">候選 <strong style="color:#fff;">${d.candidateCount}</strong> 檔</span>
        <span style="color:#aaa;">法人佔比 <strong style="color:#FFD54F;">${d.avgInstRatio}%</strong></span>
        <span style="color:#aaa;">20D <strong style="color:${d.avgReturn20>=0?'#ff4444':'#44ff44'}">${d.avgReturn20>=0?'+':''}${d.avgReturn20}%</strong></span>
        <span style="color:#aaa;">60D <strong style="color:${d.avgReturn60>=0?'#ff4444':'#44ff44'}">${d.avgReturn60>=0?'+':''}${d.avgReturn60}%</strong></span>`;

    if (d.candidateCount < 2) {
        tbody.innerHTML = `<tr><td colspan="11" style="text-align:center; color:#666; padding:16px; font-size:0.78rem;">樣本不足，僅供參考</td></tr>`;
    } else {
        tbody.innerHTML = d.stocks.map(s => {
            const sc = _strategyStateColor(s.strategyState);
            const bias = s.bias20 || 0;
            const r20  = s.return20 || 0;
            const sl   = s.stopLossPercent || 0;
            const resonance = s.hasIndustryResonance ? '<span style="color:#FF6B35; font-size:0.68rem;">🔥共振</span>' : '';
            return `
            <tr style="border-bottom:1px solid #1a1a1a; cursor:pointer;" onclick="openStockDrawer(${JSON.stringify(s).replace(/"/g,'&quot;')})">
                <td style="padding:7px 8px; color:#aaa; white-space:nowrap;">${s.stockCode}</td>
                <td style="padding:7px 8px; color:#fff; white-space:nowrap;">${s.stockName}</td>
                <td style="padding:7px 8px; text-align:center;"><span style="background:${sc}22; border:1px solid ${sc}; color:${sc}; font-size:0.68rem; padding:1px 6px; border-radius:8px; white-space:nowrap;">${s.strategyStateLabel || s.strategyState}</span></td>
                <td style="padding:7px 8px; text-align:right; color:#4facfe; font-weight:bold;">${s.score}</td>
                <td style="padding:7px 8px; text-align:right; color:#fff;">${(s.closePrice||0).toFixed(2)}</td>
                <td style="padding:7px 8px; text-align:right; color:${bias>=10?'#ffd233':'#26de81'};">${bias>=0?'+':''}${bias}%</td>
                <td style="padding:7px 8px; text-align:right; color:${r20>=0?'#ff4444':'#44ff44'};">${r20>=0?'+':''}${r20}%</td>
                <td style="padding:7px 8px; text-align:right; color:#FFD54F;">${(s.institutionBuyRatio5||0).toFixed(2)}%</td>
                <td style="padding:7px 8px; text-align:right; color:${sl<-6?'#ff4444':'#888'};">${sl.toFixed(2)}%</td>
                <td style="padding:7px 8px; color:#aaa; font-size:0.72rem; white-space:nowrap;">${s.entryPatternLabel || s.entryPattern || '--'}</td>
                <td style="padding:7px 8px;">${resonance}</td>
            </tr>`;
        }).join('');
    }
}

// ── 篩選 chip 與排序 ─────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.ind-filter-chip').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.ind-filter-chip').forEach(b => {
                b.style.background = 'none'; b.style.color = '#888';
                b.style.borderColor = '#333';
            });
            btn.style.background = 'rgba(79,172,254,0.2)';
            btn.style.color      = '#4facfe';
            btn.style.borderColor = 'rgba(79,172,254,0.5)';
            _industryFilter = btn.dataset.filter;
            _renderHeatmap();
        });
    });

    const sortSel = document.getElementById('industry-sort-select');
    if (sortSel) {
        sortSel.addEventListener('change', () => {
            _industrySort = sortSel.value;
            _renderHeatmap();
        });
    }

    const refreshBtn = document.getElementById('refresh-industry-btn');
    if (refreshBtn) refreshBtn.onclick = loadIndustryRankings;
});

// ── 自由人左側面板 ──────────────────────────────────────────────

let _flAmpPeriod = 'day';

function flSelectTxfTab(tab) {
    document.querySelectorAll('.fl-txf-tab-btn').forEach(btn => btn.classList.remove('active'));
    const target = document.getElementById(tab === 'day' ? 'fl-txf-tab-day' : 'fl-txf-tab-all');
    if (target) target.classList.add('active');
}

function flSelectAmpPeriod(period) {
    _flAmpPeriod = period;
    document.querySelectorAll('.fl-amp-period-btn').forEach(btn => btn.classList.remove('active'));
    const map = { day: 'fl-amp-tab-day', week: 'fl-amp-tab-week', month: 'fl-amp-tab-month' };
    const target = document.getElementById(map[period]);
    if (target) target.classList.add('active');
    flLoadAmplitude(period);
}

function flSetAmpValue(id, val, fallback = '--') {
    const el = document.getElementById(id);
    if (el) el.textContent = (val !== null && val !== undefined) ? val : fallback;
}

async function flLoadAmplitude(period = 'day') {
    try {
        const res = await fetch(`/api/txf_amplitude?period=${period}`);
        if (!res.ok) return;
        const d = await res.json();
        if (d.error) return;

        const periodLabel = { day: '日', week: '週', month: '月' }[period] || '日';
        const todayLabel  = { day: '本日震幅', week: '本週震幅', month: '本月震幅' }[period] || '本日震幅';

        // 更新標題文字以反映週期
        const sectionHeader = document.querySelector('#fl-amplitude-section .fl-left-section-header');
        if (sectionHeader) {
            const n = d.days || 20;
            sectionHeader.textContent = `${periodLabel}震幅統計(近${n}${periodLabel})`;
        }
        // 更新「本日/週/月」標籤
        const todayLabelEl = document.querySelector('#fl-amplitude-section .fl-amp-today-label');
        if (todayLabelEl) todayLabelEl.textContent = todayLabel;

        flSetAmpValue('fl-amp-max',   d.amp_max);
        flSetAmpValue('fl-amp-large', d.amp_large);
        flSetAmpValue('fl-amp-avg',   d.amp_avg);
        flSetAmpValue('fl-amp-small', d.amp_small);
        flSetAmpValue('fl-amp-min',   d.amp_min);
        flSetAmpValue('fl-amp-today', d.amp_today);
    } catch (e) {
        console.warn('[FL] 震幅統計載入失敗:', e);
    }
}

function flInitLeftPanel() {
    flLoadAmplitude(_flAmpPeriod);
}

let _flAmpTimer = null;
function flStartAmplitudeRefresh() {
    flLoadAmplitude(_flAmpPeriod);
    if (_flAmpTimer) clearInterval(_flAmpTimer);
    _flAmpTimer = setInterval(() => {
        if (_flAmpPeriod === 'day') flLoadAmplitude('day');
    }, 60000);
}
function flStopAmplitudeRefresh() {
    if (_flAmpTimer) { clearInterval(_flAmpTimer); _flAmpTimer = null; }
}

// ══════════════════════════════════════════════════════════════════════════════
// 🎯 明日策略選股 — Tomorrow Strategy
// ══════════════════════════════════════════════════════════════════════════════

// 大盤狀態色彩對應
const _REGIME_COLOR = {
    strong_bull:     '#26de81',
    healthy_pullback:'#4facfe',
    high_overheated: '#ff9f43',
    weak_bounce:     '#ffd233',
    bear_break60:    '#ff4444',
};
const _REGIME_BG = {
    strong_bull:     'rgba(38,222,129,0.08)',
    healthy_pullback:'rgba(79,172,254,0.08)',
    high_overheated: 'rgba(255,159,67,0.08)',
    weak_bounce:     'rgba(255,210,51,0.08)',
    bear_break60:    'rgba(255,68,68,0.08)',
};

// 個股分級顏色
const _GRADE_COLOR = {
    A:  '#26de81',
    B1: '#4facfe',
    B2: '#ff9f43',
    C:  '#ff4444',
};

function _gradeTag(grade, label) {
    const c = _GRADE_COLOR[grade] || '#888';
    return `<span style="background:${c}22; color:${c}; border:1px solid ${c}55;
        font-size:0.7rem; padding:1px 7px; border-radius:10px; font-weight:bold;
        white-space:nowrap;">${label || grade}</span>`;
}

function _macdTag(status) {
    const map = {
        '負柱收斂': '#26de81', '正柱放大': '#4facfe', '正柱收斂': '#4facfe',
        '正柱': '#4facfe', '負柱擴大': '#ff4444', '負柱': '#ff9f43',
    };
    const c = map[status] || '#888';
    return `<span style="color:${c}; font-size:0.73rem;">${status}</span>`;
}

function _volTag(status) {
    const map = {
        '量縮': '#26de81', '放量': '#4facfe', '量平': '#888',
        '下跌放量': '#ff4444', '高檔爆量長上影': '#ff4444',
    };
    const c = map[status] || '#888';
    return `<span style="color:${c}; font-size:0.73rem;">${status}</span>`;
}

function _rrTag(rr, valid) {
    if (!valid || rr === null || rr === undefined) return '<span style="color:#555;">N/A</span>';
    const c = rr >= 2.0 ? '#26de81' : rr >= 1.5 ? '#4facfe' : rr >= 1.0 ? '#ff9f43' : '#ff4444';
    return `<span style="color:${c}; font-weight:bold;">${rr.toFixed(1)}x</span>`;
}

function _liqTag(level) {
    const map = {
        'high':           ['高', '#26de81'],
        'normal':         ['普通', '#4facfe'],
        'low_amount_pass':['低張數', '#ff9f43'],
        'low':            ['不足', '#ff4444'],
    };
    const [label, c] = map[level] || ['—', '#555'];
    return `<span style="color:${c}; font-size:0.73rem;">${label}</span>`;
}

function _slopeTag(v) {
    if (v === null || v === undefined) return '—';
    const c = v > 0 ? '#26de81' : v < 0 ? '#ff4444' : '#888';
    return `<span style="color:${c};">${v >= 0 ? '+' : ''}${v.toFixed(2)}</span>`;
}

function _pct(v, digits = 1) {
    if (v === null || v === undefined) return '—';
    const s = (v >= 0 ? '+' : '') + v.toFixed(digits) + '%';
    const c = v > 0 ? '#26de81' : v < 0 ? '#ff4444' : '#888';
    return `<span style="color:${c};">${s}</span>`;
}

// ── 大盤狀態卡片 ──────────────────────────────────────────────────────────────
function _renderRegimeCard(regime) {
    const card = document.getElementById('tomorrow-regime-card');
    if (!card) return;

    const color  = _REGIME_COLOR[regime.status] || '#888';
    const bg     = _REGIME_BG[regime.status]    || 'rgba(136,136,136,0.05)';
    const m      = regime.metrics || {};
    const hasData = m.data_available;

    const metricLine = hasData
        ? `<span style="margin-right:18px;">指數收盤 <b style="color:#fff;">${(m.index_close || 0).toLocaleString()}</b></span>
           <span style="margin-right:18px;">20日成本 <b style="color:#fff;">${(m.cost20 || 0).toLocaleString()}</b>（${m.dist_cost20_pct >= 0 ? '+' : ''}${(m.dist_cost20_pct || 0).toFixed(1)}%）</span>
           <span style="margin-right:18px;">60日成本 <b style="color:#fff;">${(m.cost60 || 0).toLocaleString()}</b></span>
           <span style="margin-right:18px;">MACD <b style="color:${color};">${m.macd_status || '—'}</b></span>
           <span>量能 <b style="color:${m.vol_shrinking === null ? '#555' : m.vol_shrinking ? '#26de81' : '#ff9f43'};">${m.market_volume_status || (m.vol_shrinking ? '量縮' : '量未縮')}</b></span>`
        : '<span style="color:#555;">大盤資料不足，顯示預設狀態</span>';

    card.innerHTML = `
    <div style="background:${bg}; border:1px solid ${color}44; border-left:4px solid ${color};
        border-radius:12px; padding:16px 20px; box-shadow:0 4px 15px rgba(0,0,0,0.4);">
        <div style="display:flex; align-items:center; gap:12px; margin-bottom:12px; flex-wrap:wrap;">
            <div style="font-size:1.1rem; font-weight:bold; color:${color};">${regime.label}</div>
            <div style="font-size:0.75rem; color:#666; flex:1; min-width:200px;">${metricLine}</div>
        </div>
        <div style="display:grid; grid-template-columns:repeat(auto-fit, minmax(200px,1fr)); gap:10px; font-size:0.8rem;">
            <div style="background:#0d0d0d; border-radius:8px; padding:10px;">
                <div style="color:#666; font-size:0.7rem; margin-bottom:4px;">明日策略</div>
                <div style="color:#ccc;">${regime.strategy}</div>
            </div>
            <div style="background:#0d0d0d; border-radius:8px; padding:10px;">
                <div style="color:#26de81; font-size:0.7rem; margin-bottom:4px;">✅ 可買類型</div>
                <div style="color:#ccc;">${regime.can_buy}</div>
            </div>
            <div style="background:#0d0d0d; border-radius:8px; padding:10px;">
                <div style="color:#ff4444; font-size:0.7rem; margin-bottom:4px;">🚫 禁止類型</div>
                <div style="color:#ccc;">${regime.forbidden}</div>
            </div>
            <div style="background:#0d0d0d; border-radius:8px; padding:10px;">
                <div style="color:#ff9f43; font-size:0.7rem; margin-bottom:4px;">💼 倉位建議</div>
                <div style="color:#ccc;">${regime.position}</div>
            </div>
        </div>
        <div style="margin-top:10px; font-size:0.73rem; color:#555;">
            判斷依據：${regime.basis}
        </div>
    </div>`;

    card.style.display = 'block';
}

// ── 明日可買候選表格 ──────────────────────────────────────────────────────────
function _renderBuyTable(candidates) {
    const tbody   = document.getElementById('tomorrow-buy-tbody');
    const section = document.getElementById('tomorrow-buy-section');
    const title   = document.getElementById('tomorrow-buy-title');
    if (!tbody || !section) return;

    if (title) title.innerHTML = `✅ 明日進場觀察候選 <span style="font-size:0.8rem; color:#888; font-weight:normal;">（${candidates.length} 檔）</span><span style="font-size:0.72rem; color:#555; margin-left:10px; font-weight:normal;">※ 仍需明日確認K棒與停損風報比，非無腦買進</span>`;

    if (!candidates.length) {
        tbody.innerHTML = '<tr><td colspan="20" style="text-align:center; color:#555; padding:30px; font-size:0.8rem;">目前無符合條件的進場觀察候選（大盤條件不允許或無個股通過篩選）</td></tr>';
        section.style.display = 'block';
        return;
    }

    let rows = '';
    const reasonsHtml = [];

    candidates.forEach(s => {
        const rowBg = s.rank % 2 === 0 ? '#0d0d0d' : 'transparent';
        const incl   = (s.include_reasons || []).join('、') || '—';

        const freshColor = s.data_freshness_status === '同步' ? '#26de81' : '#ff4444';
        rows += `<tr style="border-bottom:1px solid #1a1a1a; background:${rowBg};">
            <td style="padding:7px 6px; text-align:center; color:#555;">${s.rank}</td>
            <td style="padding:7px 6px; font-weight:bold; color:#fff;">${s.symbol}</td>
            <td style="padding:7px 6px; color:#ccc; white-space:nowrap;">${s.name}</td>
            <td style="padding:7px 6px; color:#666; font-size:0.73rem; white-space:nowrap;">${s.industry || '—'}</td>
            <td style="padding:7px 6px; text-align:center;">${_gradeTag(s.grade, s.grade_label)}</td>
            <td style="padding:7px 6px; text-align:center;">
                <span style="font-weight:bold; color:${s.score >= 80 ? '#26de81' : s.score >= 60 ? '#4facfe' : '#ff9f43'};">${s.score}</span>
            </td>
            <td style="padding:7px 6px; color:#ff9f43; font-size:0.73rem; min-width:90px;">${s.buy_method || '—'}</td>
            <td style="padding:7px 6px; text-align:right; font-weight:bold; color:#fff;">${s.close}</td>
            <td style="padding:7px 6px; text-align:right; color:#aaa;">${s.cost_20}</td>
            <td style="padding:7px 6px; text-align:right; color:#aaa;">${s.cost_60}</td>
            <td style="padding:7px 6px; text-align:right;">${_pct(s.dist_cost20_pct)}</td>
            <td style="padding:7px 6px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:7px 6px; text-align:center;">${_volTag(s.volume_status)}</td>
            <td style="padding:7px 6px; text-align:right; color:#ff4444;">${s.stop_price}</td>
            <td style="padding:7px 6px; text-align:right; color:#888;">${s.resistance_price}</td>
            <td style="padding:7px 6px; text-align:right;">${_rrTag(s.risk_reward, s.rr_valid)}</td>
            <td style="padding:7px 6px; text-align:center; color:#666; font-size:0.72rem; white-space:nowrap;">${s.last_kbar_date || '—'}</td>
            <td style="padding:7px 6px; text-align:center;">${_liqTag(s.liquidity_level)}</td>
            <td style="padding:7px 6px; text-align:right;">${_slopeTag(s.cost20_slope)}</td>
            <td style="padding:7px 6px; text-align:center; color:${freshColor}; font-size:0.72rem;">${s.data_freshness_status || '—'}</td>
        </tr>`;

        // 入選原因 + 進場條件（每行展示）
        reasonsHtml.push(`
            <div style="background:#0a0a0a; border:1px solid #1a1a1a; border-radius:6px;
                padding:8px 12px; font-size:0.73rem; margin-bottom:6px;">
                <span style="font-weight:bold; color:#fff;">${s.symbol} ${s.name}</span>
                <span style="color:#555; margin:0 8px;">|</span>
                <span style="color:#26de81;">進場：</span><span style="color:#aaa;">${s.entry_condition || '—'}</span>
                <br>
                <span style="color:#555; font-size:0.7rem; margin-top:3px; display:inline-block;">
                    入選理由：${incl}
                </span>
            </div>`);
    });

    tbody.innerHTML = rows;
    const reasonsEl = document.getElementById('tomorrow-buy-reasons');
    if (reasonsEl) reasonsEl.innerHTML = reasonsHtml.join('');
    section.style.display = 'block';
}

// ── ETF 候選表格 ─────────────────────────────────────────────────────────────
function _renderEtfTable(candidates) {
    const tbody   = document.getElementById('tomorrow-etf-tbody');
    const section = document.getElementById('tomorrow-etf-section');
    const title   = document.getElementById('tomorrow-etf-title');
    if (!tbody || !section) return;

    if (title) title.innerHTML = `📊 ETF 候選 <span style="font-size:0.8rem; color:#888; font-weight:normal;">（${candidates.length} 檔，僅供參考，不混入普通股可買）</span>`;

    if (!candidates.length) {
        section.style.display = 'none';
        return;
    }

    let rows = '';
    candidates.forEach(s => {
        const rowBg = s.rank % 2 === 0 ? '#0d0d0d' : 'transparent';
        rows += `<tr style="border-bottom:1px solid #1a1a1a; background:${rowBg};">
            <td style="padding:7px 6px; text-align:center; color:#555;">${s.rank}</td>
            <td style="padding:7px 6px; font-weight:bold; color:#ffd233;">${s.symbol}</td>
            <td style="padding:7px 6px; color:#ccc; white-space:nowrap;">${s.name}</td>
            <td style="padding:7px 6px; color:#666; font-size:0.73rem; white-space:nowrap;">${s.industry || '—'}</td>
            <td style="padding:7px 6px; text-align:center;">${_gradeTag(s.grade, s.grade_label)}</td>
            <td style="padding:7px 6px; text-align:center;">
                <span style="color:${s.score >= 60 ? '#4facfe' : '#888'};">${s.score}</span>
            </td>
            <td style="padding:7px 6px; text-align:right; font-weight:bold; color:#fff;">${s.close}</td>
            <td style="padding:7px 6px; text-align:right; color:#aaa;">${s.cost_20 ?? '—'}</td>
            <td style="padding:7px 6px; text-align:right;">${_pct(s.dist_cost20_pct)}</td>
            <td style="padding:7px 6px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:7px 6px; text-align:center;">${_volTag(s.volume_status)}</td>
            <td style="padding:7px 6px; text-align:right; color:#666; font-size:0.72rem;">${s.volume_ma20 != null ? Math.round(s.volume_ma20) : '—'}</td>
        </tr>`;
    });

    tbody.innerHTML = rows;
    section.style.display = 'block';
}

// ── 高優先觀察表格 ────────────────────────────────────────────────────────────
function _renderHighWatchTable(candidates) {
    const tbody   = document.getElementById('tomorrow-high-watch-tbody');
    const section = document.getElementById('tomorrow-high-watch-section');
    const title   = document.getElementById('tomorrow-high-watch-title');
    if (!tbody || !section) return;

    if (title) title.innerHTML = `👁 高優先觀察 <span style="font-size:0.8rem; color:#888; font-weight:normal;">（${candidates.length} 檔）</span>`;

    if (!candidates.length) {
        tbody.innerHTML = '<tr><td colspan="11" style="text-align:center; color:#555; padding:30px; font-size:0.8rem;">無高優先觀察候選</td></tr>';
        section.style.display = 'block';
        return;
    }

    let rows = '';
    candidates.forEach(s => {
        const rowBg = s.rank % 2 === 0 ? '#0d0d0d' : 'transparent';
        rows += `<tr style="border-bottom:1px solid #1a1a1a; background:${rowBg};">
            <td style="padding:7px 6px; text-align:center; color:#555;">${s.rank}</td>
            <td style="padding:7px 6px; font-weight:bold; color:#fff;">${s.symbol}</td>
            <td style="padding:7px 6px; color:#ccc; white-space:nowrap;">${s.name}</td>
            <td style="padding:7px 6px; color:#666; font-size:0.73rem; white-space:nowrap;">${s.industry || '—'}</td>
            <td style="padding:7px 6px; text-align:center;">${_gradeTag(s.grade, s.grade_label)}</td>
            <td style="padding:7px 6px; text-align:center;">
                <span style="color:${s.score >= 60 ? '#4facfe' : '#888'};">${s.score}</span>
            </td>
            <td style="padding:7px 6px; text-align:right; font-weight:bold; color:#fff;">${s.close}</td>
            <td style="padding:7px 6px; text-align:right;">${_pct(s.dist_cost20_pct)}</td>
            <td style="padding:7px 6px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:7px 6px; text-align:center;">${_volTag(s.volume_status)}</td>
            <td style="padding:7px 6px; color:#888; font-size:0.73rem; max-width:220px; word-break:break-all;">${s.entry_condition || '—'}</td>
        </tr>`;
    });

    tbody.innerHTML = rows;
    section.style.display = 'block';
}

// ── 其他觀察（折疊） ──────────────────────────────────────────────────────────
function _renderOtherWatchTable(candidates) {
    const tbody   = document.getElementById('tomorrow-other-watch-tbody');
    const summary = document.getElementById('tomorrow-other-watch-summary');
    if (!tbody) return;

    if (summary) {
        summary.textContent = `▶ 其他觀察（${candidates.length} 檔，點擊展開）`;
    }

    if (!candidates.length) {
        tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; color:#444; padding:16px;">無其他觀察</td></tr>';
        return;
    }

    let rows = '';
    candidates.forEach(s => {
        const rowBg = (s.rank || 0) % 2 === 0 ? '#0d0d0d' : 'transparent';
        const gc = _GRADE_COLOR[s.grade] || '#555';
        rows += `<tr style="border-bottom:1px solid #111; background:${rowBg};">
            <td style="padding:5px 6px; text-align:center; color:#555;">${s.rank || ''}</td>
            <td style="padding:5px 6px; color:#aaa; font-weight:bold;">${s.symbol}</td>
            <td style="padding:5px 6px; color:#777;">${s.name}</td>
            <td style="padding:5px 6px; color:#555; font-size:0.7rem;">${s.industry || '—'}</td>
            <td style="padding:5px 6px; text-align:center;"><span style="color:${gc}; font-size:0.7rem;">${s.grade_label || s.grade || '—'}</span></td>
            <td style="padding:5px 6px; text-align:center; color:#666;">${s.score ?? '—'}</td>
            <td style="padding:5px 6px; text-align:right; color:#aaa;">${s.close}</td>
            <td style="padding:5px 6px; text-align:right;">${_pct(s.dist_cost20_pct)}</td>
            <td style="padding:5px 6px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:5px 6px; color:#555; font-size:0.7rem; max-width:200px; word-break:break-all;">${s.entry_condition || '—'}</td>
        </tr>`;
    });

    tbody.innerHTML = rows;
}

// ── 排除清單（折疊，含完整 debug 欄位） ───────────────────────────────────────
function _renderExcludedList(excluded, stats) {
    const tbody   = document.getElementById('tomorrow-excluded-tbody');
    const summary = document.getElementById('tomorrow-excluded-summary');
    if (!tbody) return;

    const cnt = excluded.length;
    if (summary) {
        summary.textContent = `▶ 排除清單（${cnt} 檔，點擊展開）`;
    }

    if (!cnt) {
        tbody.innerHTML = '<tr><td colspan="10" style="text-align:center; color:#444; padding:16px;">無排除股票</td></tr>';
        return;
    }

    // 依 score 降序，讓高分但被排除的股票排在前面
    const sorted = [...excluded].sort((a, b) => (b.score || 0) - (a.score || 0));

    let rows = '';
    sorted.forEach(s => {
        const reason = (s.exclude_reasons || []).join('；') || '—';
        const gc = _GRADE_COLOR[s.grade] || '#555';
        const instColor = s.instrument_type === 'etf' ? '#ffd233'
            : s.instrument_type === 'reverse_etf' ? '#ff4444'
            : s.instrument_type === 'warrant'     ? '#888'
            : '#555';
        rows += `<tr style="border-bottom:1px solid #111;">
            <td style="padding:5px 6px; color:#888;">${s.symbol}</td>
            <td style="padding:5px 6px; color:#666; white-space:nowrap;">${s.name}</td>
            <td style="padding:5px 6px; text-align:center; font-size:0.68rem; color:${instColor};">${s.instrument_type || '—'}</td>
            <td style="padding:5px 6px; text-align:center;"><span style="color:${gc}; font-size:0.7rem;">${s.grade_label || s.grade || '—'}</span></td>
            <td style="padding:5px 6px; text-align:center; color:${(s.score || 0) >= 60 ? '#4facfe' : '#555'};">${s.score ?? '—'}</td>
            <td style="padding:5px 6px; text-align:right; color:#777;">${s.close ?? '—'}</td>
            <td style="padding:5px 6px; text-align:right;">${s.dist_cost20_pct != null ? _pct(s.dist_cost20_pct) : '—'}</td>
            <td style="padding:5px 6px; text-align:center;">${s.macd_status ? _macdTag(s.macd_status) : '—'}</td>
            <td style="padding:5px 6px; text-align:right;">${_rrTag(s.risk_reward, s.risk_reward != null)}</td>
            <td style="padding:5px 6px; color:#555; font-size:0.7rem; max-width:260px; word-break:break-all;">${reason}</td>
        </tr>`;
    });

    tbody.innerHTML = rows;
}

// ── 主載入函式 ────────────────────────────────────────────────────────────────
async function loadTomorrowStrategy() {
    const loading       = document.getElementById('tomorrow-loading');
    const regCard       = document.getElementById('tomorrow-regime-card');
    const buySection    = document.getElementById('tomorrow-buy-section');
    const etfSection    = document.getElementById('tomorrow-etf-section');
    const highWatchSect = document.getElementById('tomorrow-high-watch-section');
    const emptyEl       = document.getElementById('tomorrow-empty');
    const dateLabel     = document.getElementById('stock-rank-date');

    // 重置
    if (loading)       loading.style.display       = 'block';
    if (regCard)       regCard.style.display        = 'none';
    if (buySection)    buySection.style.display     = 'none';
    if (etfSection)    etfSection.style.display     = 'none';
    if (highWatchSect) highWatchSect.style.display  = 'none';
    if (emptyEl)       emptyEl.style.display        = 'none';

    try {
        const res  = await fetch('/api/tomorrow_strategy');
        const _rawText2 = await res.text();
        let data;
        try { data = JSON.parse(_rawText2); } catch(_e) {
            throw new Error(`tomorrow_strategy API 回傳非 JSON：${_rawText2.slice(0, 120)}`);
        }

        if (loading) loading.style.display = 'none';

        if (data.status !== 'success') {
            if (emptyEl) { emptyEl.style.display = 'block'; emptyEl.textContent = `計算失敗：${data.detail || '未知錯誤'}`; }
            return;
        }

        // 資料日期
        if (dateLabel && data.data_date) {
            dateLabel.textContent = `📅 ${data.data_date}`;
        }

        const stats = data.stats || {};
        if (stats.total_analyzed === 0 && !data.market_regime?.metrics?.data_available) {
            if (emptyEl) emptyEl.style.display = 'block';
            return;
        }

        // 大盤狀態卡片
        if (data.market_regime) _renderRegimeCard(data.market_regime);

        // 四個清單
        _renderBuyTable(data.buy_candidates       || []);
        _renderEtfTable(data.etf_candidates       || []);
        _renderHighWatchTable(data.high_priority_watch || []);
        _renderOtherWatchTable(data.other_watch   || []);
        _renderExcludedList(data.excluded         || [], stats);

    } catch (e) {
        if (loading) loading.style.display = 'none';
        if (emptyEl) {
            emptyEl.style.display  = 'block';
            emptyEl.innerHTML = `⚠️ 載入失敗：${e.message}<br><span style="font-size:0.75rem; color:#555;">請確認伺服器是否正常運行</span>`;
        }
        console.error('[TomorrowStrategy] 載入失敗:', e);
    }
}

// ════════════════════════════════════════════════════════════════════════════
// 整合選股 (Integrated Strategy)
// ════════════════════════════════════════════════════════════════════════════

function _fmtDist(v) {
    if (v == null) return '—';
    const n = parseFloat(v);
    return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
}
function _fmtRR(v) {
    if (v == null) return '—';
    return parseFloat(v).toFixed(1);
}
function _fmtPct(v) {
    if (v == null) return '—';
    const n = parseFloat(v);
    return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
}
function _gradeTag(grade, color) {
    const c = color || (grade === 'A' ? '#26de81' : grade === 'B1' ? '#4facfe' : '#ff9f43');
    return `<span style="background:rgba(${_hexToRgb(c)},0.15); color:${c}; border:1px solid ${c}40; border-radius:4px; padding:1px 6px; font-size:0.72rem; font-weight:bold;">${grade || '—'}</span>`;
}
function _hexToRgb(hex) {
    const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
    return `${r},${g},${b}`;
}
function _macdTag(s) {
    const colorMap = {'負柱收斂':'#26de81','正柱放大':'#4facfe','正柱':'#4facfe','正柱收斂':'#88ccff','負柱':'#ff9f43','負柱擴大':'#ff4444'};
    const c = colorMap[s] || '#888';
    return s ? `<span style="color:${c}; font-size:0.72rem;">${s}</span>` : '—';
}
function _resonanceTag(v) {
    return v ? `<span style="color:#ffd233; font-size:0.75rem;">⚡共振</span>` : `<span style="color:#333; font-size:0.72rem;">—</span>`;
}

// ── 整合選股：明日可買 ────────────────────────────────────────────────────────

function _integratedEsc(v) {
    return String(v == null ? '' : v).replace(/[&<>"]/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[ch]));
}
function _moneydjPeriodText(period) {
    const p = String(period || '5D').toUpperCase();
    return ({'1D':'近1日','5D':'近5日','10D':'近10日','20D':'近20日'})[p] || p;
}
function _moneydjRiskText(risk) {
    const key = String(risk || '').trim();
    return ({
        'stale_data': '資料日期不一致',
        'broker_sell_pressure': '分點賣壓 / 多空分歧',
        'broker_accumulation': '分點偏多',
        'broker_daytrade': '疑似隔日沖',
        'broker_distributed': '買盤分散'
    })[key] || '無明顯風險';
}

function _moneydjCommentSummary(text, maxLen = 36) {
    const raw = String(text || '').trim();
    if (!raw) return '';
    return raw.length > maxLen ? `${raw.slice(0, maxLen)}...` : raw;
}

function _renderMoneydjInfo(s) {
    const valid = s && s.moneydj_date_valid === true;
    const commentRaw = String(s?.broker_comment || '').trim();
    const comment = _integratedEsc(commentRaw);
    if (!valid) {
        return `<div style="margin-top:4px; color:#777; font-size:0.68rem; line-height:1.35; white-space:normal; overflow:visible;">MoneyDJ資料未同步或日期不一致，不參與加分</div>`;
    }

    const bonus = Number(s.broker_bonus || 0);
    const bonusText = bonus > 0 ? `+${bonus}` : `${bonus}`;
    const periodText = _moneydjPeriodText(s.moneydj_period_label);
    const periodDetailText = _integratedEsc(periodText);
    const endDate = _integratedEsc(s.moneydj_end_date || '--');
    const riskRaw = String(s.broker_risk || '').trim();
    const risk = _integratedEsc(_moneydjRiskText(riskRaw));
    const tags = Array.isArray(s.broker_tags) ? s.broker_tags.filter(Boolean) : [];
    const tagText = tags.length ? tags.join('、') : '無';
    const tagsEsc = _integratedEsc(tagText);
    const summaryText = _integratedEsc(_moneydjCommentSummary(commentRaw));
    const bonusColor = bonus > 0 ? '#26de81' : (bonus < 0 ? '#ff9f43' : '#888');
    const bonusBg = bonus > 0 ? 'rgba(38,222,129,0.12)' : (bonus < 0 ? 'rgba(255,159,67,0.12)' : 'rgba(120,120,120,0.10)');
    const bonusBorder = bonus > 0 ? 'rgba(38,222,129,0.45)' : (bonus < 0 ? 'rgba(255,159,67,0.45)' : 'rgba(120,120,120,0.35)');
    const bonusLabel = bonus > 0 ? `分點加分 ${bonusText}` : (bonus < 0 ? `分點扣分 ${bonusText}` : `分點 ${bonusText}`);
    const riskColor = riskRaw === 'stale_data' ? '#777' : (bonus < 0 ? '#ffb86c' : '#888');
    const riskBg = riskRaw === 'stale_data' ? 'rgba(120,120,120,0.10)' : (bonus < 0 ? 'rgba(255,159,67,0.10)' : 'rgba(120,120,120,0.08)');
    const riskBorder = riskRaw === 'stale_data' ? 'rgba(120,120,120,0.35)' : (bonus < 0 ? 'rgba(255,159,67,0.38)' : 'rgba(120,120,120,0.28)');
    const tagHtml = tags.slice(0, 2).map(t => `<span style="display:inline-block; margin-left:4px; padding:1px 5px; border-radius:4px; border:1px solid ${riskBorder}; color:${riskColor}; background:${riskBg}; font-size:0.64rem; white-space:nowrap;">${_integratedEsc(t)}</span>`).join('');
    const riskTag = riskRaw ? `<span style="display:inline-block; margin-left:4px; padding:1px 5px; border-radius:4px; border:1px solid ${riskBorder}; color:${riskColor}; background:${riskBg}; font-size:0.64rem; white-space:nowrap;">${risk}</span>` : '';
    const commentSummary = summaryText ? `<div style="margin-top:2px; color:#777; white-space:normal; overflow:visible;">${summaryText}</div>` : '';

    return `<details style="margin-top:4px; font-size:0.68rem; line-height:1.35; color:#777; max-width:280px; white-space:normal; overflow:visible;">
        <summary style="cursor:pointer; list-style-position:inside; white-space:normal; overflow:visible;">
            <span style="color:#888;">分點：</span><span style="color:${bonusColor}; font-weight:600;">${bonusText}</span><span style="color:#666;">｜${periodText}｜資料日 ${endDate}</span>
            <span style="display:inline-block; margin-left:4px; padding:1px 5px; border-radius:4px; border:1px solid ${bonusBorder}; color:${bonusColor}; background:${bonusBg}; font-size:0.64rem; white-space:nowrap;">${bonusLabel}</span>${riskTag}${tagHtml}
            ${commentSummary}
            <span style="display:inline-block; margin-top:2px; color:#4facfe; font-size:0.64rem;">展開</span>
        </summary>
        <div style="margin-top:5px; padding:6px 7px; border:1px solid rgba(120,120,120,0.18); border-radius:6px; background:rgba(255,255,255,0.03); color:#aaa; white-space:normal; overflow:visible; word-break:break-word; overflow-wrap:anywhere;">
            <div><span style="color:#777;">分點分數：</span><span style="color:${bonusColor}; font-weight:600;">${bonusText}</span></div>
            <div><span style="color:#777;">分析區間：</span>${periodDetailText}</div>
            <div><span style="color:#777;">資料日期：</span>${endDate}</div>
            <div><span style="color:#777;">分點風險：</span>${risk}</div>
            <div><span style="color:#777;">分點標籤：</span>${tagsEsc}</div>
            <div style="margin-top:6px; color:#777;">完整分析：</div>
            <div style="margin-top:2px; color:#ddd; white-space:normal; overflow:visible; word-break:break-word; overflow-wrap:anywhere;">${comment || 'MoneyDJ分點資料無明確說明。'}</div>
        </div>
    </details>`;
}

function _renderIntegratedBuyTable(candidates) {
    const tbody   = document.getElementById('integrated-buy-tbody');
    const section = document.getElementById('integrated-buy-section');
    const title   = document.getElementById('integrated-buy-title');
    if (!tbody || !section) return;

    if (!candidates.length) { section.style.display = 'none'; return; }

    if (title) title.innerHTML = `✅ 明日可買 <span style="background:#26de81; color:#000; border-radius:10px; padding:2px 8px; font-size:0.75rem;">${candidates.length}</span>`;

    tbody.innerHTML = candidates.map(s => {
        const slPct = s.stop_loss_pct != null ? parseFloat(s.stop_loss_pct).toFixed(1) + '%' : '—';
        const scoreColor = s.final_score >= 70 ? '#26de81' : s.final_score >= 50 ? '#ff9f43' : '#888';
        return `<tr style="border-bottom:1px solid #1a1a1a;">
            <td style="padding:5px 8px; color:#666;">${s.rank || ''}</td>
            <td style="padding:5px 8px; white-space:nowrap;">
                <span style="color:#fff; font-weight:bold;">${s.stock_id}</span>
                <span style="color:#888; font-size:0.78rem; margin-left:4px;">${s.stock_name || ''}</span>
            </td>
            <td style="padding:5px 8px; text-align:center;">${_gradeTag(s.stock_grade, s.grade_color)}</td>
            <td style="padding:5px 8px; text-align:right; color:#eee;">${s.close != null ? parseFloat(s.close).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px; text-align:right;">${_fmtDist(s.cost20_distance)}</td>
            <td style="padding:5px 8px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:5px 8px; text-align:right; color:${(s.risk_reward||0)>=1.5?'#26de81':'#888'};">${_fmtRR(s.risk_reward)}</td>
            <td style="padding:5px 8px; text-align:right; color:#ff9f43;">${slPct}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#aaa; max-width:120px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${s.institution_5d_status||''}">${s.institution_5d_status || '—'}</td>
            <td style="padding:5px 8px; text-align:center; color:#4facfe;">${s.industry_score != null ? s.industry_score.toFixed(0) : '—'}</td>
            <td style="padding:5px 8px; text-align:center;">${_resonanceTag(s.has_industry_resonance)}</td>
            <td style="padding:5px 8px; text-align:right; font-weight:bold; color:${scoreColor};">${s.final_score != null ? s.final_score : '—'}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#aaa; max-width:200px;">${s.action_suggestion || ''}${_renderMoneydjInfo(s)}</td>
        </tr>`;
    }).join('');
    section.style.display = 'block';
}

// ── 整合選股：高優先觀察 ──────────────────────────────────────────────────────
function _renderIntegratedHighWatchTable(candidates) {
    const tbody   = document.getElementById('integrated-high-watch-tbody');
    const section = document.getElementById('integrated-high-watch-section');
    const title   = document.getElementById('integrated-high-watch-title');
    if (!tbody || !section) return;

    if (!candidates.length) { section.style.display = 'none'; return; }

    if (title) title.innerHTML = `👁 高優先觀察 <span style="background:#4facfe; color:#000; border-radius:10px; padding:2px 8px; font-size:0.75rem;">${candidates.length}</span>`;

    tbody.innerHTML = candidates.map(s => {
        const scoreColor = s.final_score >= 60 ? '#4facfe' : '#888';
        return `<tr style="border-bottom:1px solid #1a1a1a;">
            <td style="padding:5px 8px; color:#666;">${s.rank || ''}</td>
            <td style="padding:5px 8px; white-space:nowrap;">
                <span style="color:#ccc; font-weight:bold;">${s.stock_id}</span>
                <span style="color:#666; font-size:0.78rem; margin-left:4px;">${s.stock_name || ''}</span>
            </td>
            <td style="padding:5px 8px; text-align:center;">${_gradeTag(s.stock_grade, s.grade_color)}</td>
            <td style="padding:5px 8px; text-align:right; color:#eee;">${s.close != null ? parseFloat(s.close).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px; text-align:right;">${_fmtDist(s.cost20_distance)}</td>
            <td style="padding:5px 8px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:5px 8px; text-align:right; color:${(s.risk_reward||0)>=1.5?'#26de81':'#888'};">${_fmtRR(s.risk_reward)}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#aaa; max-width:120px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${s.institution_5d_status||''}">${s.institution_5d_status || '—'}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#888; max-width:80px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${s.industry_status||''}">${s.industry_status || '—'}</td>
            <td style="padding:5px 8px; text-align:right; color:${scoreColor};">${s.final_score != null ? s.final_score : '—'}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#888; max-width:200px;">${s.entry_condition || s.action_suggestion || ''}${_renderMoneydjInfo(s)}</td>
        </tr>`;
    }).join('');
    section.style.display = 'block';
}

// ── 整合選股：等回測 ──────────────────────────────────────────────────────────
function _renderIntegratedWaitPullbackTable(candidates) {
    const tbody   = document.getElementById('integrated-wait-pullback-tbody');
    const section = document.getElementById('integrated-wait-pullback-section');
    const title   = document.getElementById('integrated-wait-pullback-title');
    if (!tbody || !section) return;

    if (!candidates.length) { section.style.display = 'none'; return; }

    if (title) title.innerHTML = `⏳ 等回測（強勢但不能追高）<span style="background:#ffd233; color:#000; border-radius:10px; padding:2px 8px; font-size:0.75rem; margin-left:6px;">${candidates.length}</span>`;

    tbody.innerHTML = candidates.map(s => {
        const slPct = s.stop_loss_pct != null ? parseFloat(s.stop_loss_pct).toFixed(1) + '%' : '—';
        return `<tr style="border-bottom:1px solid #1a1a1a;">
            <td style="padding:5px 8px; color:#666;">${s.rank || ''}</td>
            <td style="padding:5px 8px; white-space:nowrap;">
                <span style="color:#ffd233; font-weight:bold;">${s.stock_id}</span>
                <span style="color:#666; font-size:0.78rem; margin-left:4px;">${s.stock_name || ''}</span>
            </td>
            <td style="padding:5px 8px; text-align:center;">${_gradeTag(s.stock_grade, s.grade_color)}</td>
            <td style="padding:5px 8px; text-align:right; color:#eee;">${s.close != null ? parseFloat(s.close).toFixed(2) : '—'}</td>
            <td style="padding:5px 8px; text-align:right; color:#ff9f43;">${_fmtDist(s.cost20_distance)}</td>
            <td style="padding:5px 8px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:5px 8px; text-align:right; color:#ff9f43;">${slPct}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#aaa; max-width:120px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${s.institution_5d_status||''}">${s.institution_5d_status || '—'}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#888; max-width:80px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${s.industry_status||''}">${s.industry_status || '—'}</td>
            <td style="padding:5px 8px; text-align:center;">${_resonanceTag(s.has_industry_resonance)}</td>
            <td style="padding:5px 8px; text-align:right; color:#888;">${s.final_score != null ? s.final_score : '—'}</td>
            <td style="padding:5px 8px; font-size:0.72rem; color:#888; max-width:200px;">${s.action_suggestion || ''}${_renderMoneydjInfo(s)}</td>
        </tr>`;
    }).join('');
    section.style.display = 'block';
}

// ── 整合選股：其他觀察 ────────────────────────────────────────────────────────
function _renderIntegratedOtherWatchTable(candidates) {
    const tbody   = document.getElementById('integrated-other-watch-tbody');
    const summary = document.getElementById('integrated-other-watch-summary');
    if (!tbody) return;

    if (summary) summary.innerHTML = `▶ 其他觀察（點擊展開）<span style="color:#555; margin-left:6px;">${candidates.length} 檔</span>`;

    if (!candidates.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#444; padding:16px;">暫無資料</td></tr>';
        return;
    }
    tbody.innerHTML = candidates.map(s => `<tr style="border-bottom:1px solid #111;">
        <td style="padding:4px 6px; white-space:nowrap;">
            <span style="color:#888;">${s.stock_id}</span>
            <span style="color:#555; font-size:0.75rem; margin-left:3px;">${s.stock_name || ''}</span>
        </td>
        <td style="padding:4px 6px; text-align:center;">${_gradeTag(s.stock_grade, s.grade_color)}</td>
        <td style="padding:4px 6px; text-align:right; color:#aaa;">${s.close != null ? parseFloat(s.close).toFixed(2) : '—'}</td>
        <td style="padding:4px 6px; text-align:right; color:#666;">${_fmtDist(s.cost20_distance)}</td>
        <td style="padding:4px 6px; text-align:center;">${_macdTag(s.macd_status)}</td>
        <td style="padding:4px 6px; font-size:0.70rem; color:#666; max-width:100px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;" title="${s.institution_5d_status||''}">${s.institution_5d_status || '—'}</td>
        <td style="padding:4px 6px; text-align:right; color:#666;">${s.final_score != null ? s.final_score : '—'}</td>
        <td style="padding:4px 6px; font-size:0.70rem; color:#555; max-width:180px;">${s.action_suggestion || ''}${_renderMoneydjInfo(s)}</td>
    </tr>`).join('');
}

// ── 整合選股：排除清單 ────────────────────────────────────────────────────────
function _renderIntegratedExcludedList(excluded, summary) {
    const tbody   = document.getElementById('integrated-excluded-tbody');
    const sumEl   = document.getElementById('integrated-excluded-summary');
    if (!tbody) return;

    const cnt = (summary && summary.excluded_count != null) ? summary.excluded_count : excluded.length;
    if (sumEl) sumEl.innerHTML = `▶ 排除清單（點擊展開）<span style="color:#555; margin-left:6px;">${cnt} 檔</span>`;

    if (!excluded.length) {
        tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:#444; padding:20px;">暫無資料</td></tr>';
        return;
    }
    tbody.innerHTML = excluded.slice(0, 200).map(s => {
        const reasons = Array.isArray(s.exclude_reasons) ? s.exclude_reasons.join('；') : (s.final_reason || '');
        return `<tr style="border-bottom:1px solid #0f0f0f;">
            <td style="padding:3px 6px; color:#555; white-space:nowrap;">
                <span style="color:#555;">${s.stock_id}</span>
                <span style="color:#444; font-size:0.72rem; margin-left:3px;">${s.stock_name || ''}</span>
            </td>
            <td style="padding:3px 6px; text-align:center; color:#555;">${s.grade_label || s.stock_grade || '—'}</td>
            <td style="padding:3px 6px; text-align:right; color:#555;">${s.close != null ? parseFloat(s.close).toFixed(2) : '—'}</td>
            <td style="padding:3px 6px; text-align:right; color:#555;">${_fmtDist(s.cost20_distance || s.dist_cost20_pct)}</td>
            <td style="padding:3px 6px; text-align:center;">${_macdTag(s.macd_status)}</td>
            <td style="padding:3px 6px; text-align:center; color:#555;">${s.volume_status || '—'}</td>
            <td style="padding:3px 6px; text-align:right; color:#555;">${_fmtRR(s.risk_reward)}</td>
            <td style="padding:3px 6px; font-size:0.70rem; color:#555; max-width:220px;">${reasons}${_renderMoneydjInfo(s)}</td>
        </tr>`;
    }).join('');
}

// ── 整合選股主載入函式 ────────────────────────────────────────────────────────
async function loadIntegratedStrategy() {
    const loading          = document.getElementById('integrated-loading');
    const regCard          = document.getElementById('integrated-regime-card');
    const buySection       = document.getElementById('integrated-buy-section');
    const highWatchSection = document.getElementById('integrated-high-watch-section');
    const waitPBSection    = document.getElementById('integrated-wait-pullback-section');
    const emptyEl          = document.getElementById('integrated-empty');
    const dateLabel        = document.getElementById('stock-rank-date');

    // 重置
    [regCard, buySection, highWatchSection, waitPBSection, emptyEl].forEach(el => {
        if (el) el.style.display = 'none';
    });
    if (loading) loading.style.display = 'block';

    _loadIntegratedTgPushStatus();

    try {
        const res  = await fetch('/api/integrated-strategy');
        const _rawText3 = await res.text();
        let data;
        try { data = JSON.parse(_rawText3); } catch(_e) {
            throw new Error(`integrated-strategy API 回傳非 JSON：${_rawText3.slice(0, 120)}`);
        }

        if (loading) loading.style.display = 'none';

        if (data.status !== 'success') {
            if (emptyEl) { emptyEl.style.display = 'block'; emptyEl.textContent = `計算失敗：${data.detail || '未知錯誤'}`; }
            return;
        }

        if (dateLabel && data.data_date) {
            dateLabel.textContent = `📅 ${data.data_date}`;
        }

        const summary = data.summary || {};
        if (!summary.total_analyzed) {
            if (emptyEl) emptyEl.style.display = 'block';
            return;
        }

        // 大盤狀態卡片（重用 tomorrow 的渲染函式）
        if (data.market_regime) {
            const card = document.getElementById('integrated-regime-card');
            if (card) {
                // 直接複用 _renderRegimeCard 邏輯，但指向 integrated-regime-card
                const r = data.market_regime;
                const m = r.metrics || {};
                const dataAvailable = m.data_available !== false;
                card.style.display = 'block';
                card.innerHTML = `
                <div style="background:#111; border:1px solid ${r.color||'#444'}40; border-left:4px solid ${r.color||'#888'}; border-radius:10px; padding:14px; box-shadow:0 4px 12px rgba(0,0,0,0.4);">
                    <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
                        <div>
                            <div style="font-size:0.72rem; color:#555; margin-bottom:2px;">大盤狀態</div>
                            <div style="font-size:1.1rem; font-weight:700; color:${r.color||'#fff'};">${r.label||'—'}</div>
                        </div>
                        ${dataAvailable ? `
                        <div style="display:flex; gap:16px; flex-wrap:wrap; font-size:0.78rem; color:#aaa;">
                            <div>收盤 <strong style="color:#eee;">${m.index_close!=null?m.index_close.toLocaleString():'—'}</strong></div>
                            <div>cost20 <strong style="color:#4facfe;">${m.cost20!=null?m.cost20.toLocaleString():'—'}</strong></div>
                            <div>cost60 <strong style="color:#ff9f43;">${m.cost60!=null?m.cost60.toLocaleString():'—'}</strong></div>
                            <div>距20日 <strong style="color:${(m.dist_cost20_pct||0)>=0?'#26de81':'#ff4444'};">${m.dist_cost20_pct!=null?((m.dist_cost20_pct>=0?'+':'')+m.dist_cost20_pct.toFixed(1)+'%'):'—'}</strong></div>
                            <div>MACD <strong style="color:#888;">${m.macd_status||'—'}</strong></div>
                        </div>` : ''}
                        <div style="margin-left:auto; text-align:right;">
                            <div style="font-size:0.72rem; color:#555;">操作原則</div>
                            <div style="font-size:0.78rem; color:#888; max-width:280px; text-align:right;">${r.strategy||'—'}</div>
                        </div>
                    </div>
                    ${r.basis ? `<div style="margin-top:8px; font-size:0.73rem; color:#555; border-top:1px solid #1a1a1a; padding-top:8px;">${r.basis}</div>` : ''}
                </div>`;
            }
        }

        _renderIntegratedBuyTable(data.buy_candidates        || []);
        _renderIntegratedHighWatchTable(data.high_priority_watch || []);
        _renderIntegratedWaitPullbackTable(data.wait_pullback    || []);
        _renderIntegratedOtherWatchTable(data.other_watch        || []);
        _renderIntegratedExcludedList(data.excluded              || [], summary);

    } catch (e) {
        if (loading) loading.style.display = 'none';
        if (emptyEl) {
            emptyEl.style.display = 'block';
            emptyEl.innerHTML = `⚠️ 載入失敗：${e.message}<br><span style="font-size:0.75rem; color:#555;">請確認伺服器是否正常運行</span>`;
        }
        console.error('[IntegratedStrategy] 載入失敗:', e);
    }
}

// ── 震幅統計分頁 ──────────────────────────────────────────────────────────────

let _ampStatsTimer   = null;
let _ampStatsHelpOpen = true;
let _ampDateMode     = 'calendar_date';

function toggleAmpStatsHelp() {
    _ampStatsHelpOpen = !_ampStatsHelpOpen;
    const body   = document.getElementById('amp-stats-help-body');
    const toggle = document.getElementById('amp-stats-help-toggle');
    if (body)   body.style.display   = _ampStatsHelpOpen ? 'flex' : 'none';
    if (toggle) toggle.textContent   = _ampStatsHelpOpen ? '▲ 收合' : '▼ 展開';
}

function setAmpDateMode(mode) {
    _ampDateMode = mode;
    const btnTd = document.getElementById('amp-mode-btn-trading');
    const btnCal = document.getElementById('amp-mode-btn-calendar');
    if (btnTd)  btnTd.style.background  = mode === 'trading_date' ? '#1a4a7a' : '#0d1f2d';
    if (btnCal) btnCal.style.background = mode === 'calendar_date' ? '#1a4a7a' : '#0d1f2d';
    loadAmplitudeStatistics();
}

async function loadAmplitudeStatistics() {
    const inner = document.getElementById('amp-stats-table-inner');
    if (!inner) return;
    inner.innerHTML = '<div style="color:#555; padding:30px; text-align:center;">載入中...</div>';

    try {
        const res = await fetch(`/api/amplitude_statistics?days=20&contract=TXFR1&date_mode=${_ampDateMode}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!data.success) throw new Error(data.error || 'API error');

        const updEl = document.getElementById('amp-stats-updated-at');
        if (updEl) updEl.textContent = data.updated_at;

        renderAmplitudeStatisticsTable(data);
    } catch (e) {
        if (inner) inner.innerHTML = `<div style="color:#f55; padding:30px;">⚠️ 載入失敗：${e.message}</div>`;
        console.warn('[AmpStats] 載入失敗:', e);
    }
}

function getAmplitudeStatusClass(status) {
    return {
        super_large: 'amp-status-super-large',
        large:       'amp-status-large',
        small:       'amp-status-small',
        compressed:  'amp-status-compressed',
        normal:      'amp-status-normal',
        empty:       'amp-status-empty',
        avg:         'amp-row-avg-cell',
    }[status] || '';
}

function renderAmplitudeStatisticsTable(data) {
    const inner = document.getElementById('amp-stats-table-inner');
    if (!inner) return;

    const { columns, rows } = data;

    let html = '<table class="amp-stats-table"><thead><tr>';
    html += '<th class="amp-stats-label-col">時段</th>';
    for (const col of columns) {
        const cls = col.is_today ? 'amp-col-today' : '';
        html += `<th class="${cls}">${col.label}<br><span style="font-size:0.65rem; color:#555;">${col.weekday}</span></th>`;
    }
    html += '</tr></thead><tbody>';

    for (const row of rows) {
        const isTotal = row.key === 'total';
        const isAvg   = row.key.endsWith('_avg20');
        const rowCls  = isTotal ? 'amp-row-total' : (isAvg ? 'amp-row-avg' : '');

        html += `<tr class="${rowCls}">`;
        html += `<td class="amp-stats-label-col">${row.label}</td>`;

        for (const cell of row.cells) {
            const colIsToday = columns.find(c => c.date === cell.date)?.is_today;
            const cellCls  = getAmplitudeStatusClass(cell.status);
            const todayCls = colIsToday ? 'amp-col-today' : '';
            const val      = cell.value !== null ? cell.value : '-';
            const tip      = (cell.high && cell.low) ? ` title="H:${cell.high} L:${cell.low}"` : '';
            html += `<td class="${cellCls} ${todayCls}"${tip}>${val}</td>`;
        }

        html += '</tr>';
    }

    html += '</tbody></table>';
    inner.innerHTML = html;
}

function stopAmplitudeStatisticsAutoRefresh() {
    if (_ampStatsTimer) { clearInterval(_ampStatsTimer); _ampStatsTimer = null; }
}

async function sendAmplitudeDailyReport() {
    const btn    = document.getElementById('amp-send-tg-btn');
    const status = document.getElementById('amp-send-tg-status');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ 傳送中...'; }
    if (status) status.textContent = '';
    try {
        const res  = await fetch('/api/amplitude/send_daily_report', { method: 'POST' });
        const data = await res.json();
        if (!res.ok) {
            if (status) status.textContent = `❌ ${data.detail || '傳送失敗'}`;
            return;
        }
        if (!data.success) {
            if (status) status.textContent = `⚠️ ${data.message || '未成功'}`;
            if (data.message && data.message.includes('尚未設定')) {
                const panel = document.getElementById('amp-tg-panel');
                if (panel) {
                    panel.scrollIntoView({ behavior: 'smooth' });
                    if (!_ampTgPanelOpen) toggleAmpTgPanel();
                }
            }
            return;
        }
        if (status) status.textContent = `✅ 已傳送 ${data.sent}/${data.target_count} 個目標（${data.data_date}）`;
    } catch(e) {
        if (status) status.textContent = `❌ ${e.message}`;
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '📤 發送昨日震幅狀態'; }
    }
}

// ── 震幅 TG 接收者管理 ──────────────────────────────────────────────────────

let _ampTgPanelOpen = true;

function toggleAmpTgPanel() {
    _ampTgPanelOpen = !_ampTgPanelOpen;
    const body   = document.getElementById('amp-tg-panel-body');
    const toggle = document.getElementById('amp-tg-panel-toggle');
    if (body)   body.style.display = _ampTgPanelOpen ? 'block' : 'none';
    if (toggle) toggle.textContent = _ampTgPanelOpen ? '▲ 收合' : '▼ 展開';
}

async function loadAmplitudeTgTargets() {
    try {
        const res  = await fetch('/api/tg/targets');
        const data = await res.json();
        const amp  = (data.targets || []).filter(t => t.target_type === 'amplitude' || t.target_type === 'all');
        renderAmplitudeTgTargets(amp);
    } catch(e) { console.error('震幅 TG targets 讀取失敗', e); }
}

function renderAmplitudeTgTargets(targets) {
    const list = document.getElementById('amp-tg-targets-list');
    if (!list) return;
    if (!targets || targets.length === 0) {
        list.innerHTML = `<div style="color:#555; font-size:0.75rem; padding:6px 0;">尚未新增任何震幅接收者</div>`;
        return;
    }
    const typeLabel = { amplitude: '震幅統計', all: '全部' };
    const typeColor = { amplitude: '#4facfe', all: '#26de81' };
    list.innerHTML = targets.map(t => {
        const enabledColor = t.enabled ? '#26de81' : '#555';
        const enabledLabel = t.enabled ? '啟用中' : '已停用';
        const tt    = t.target_type || 'amplitude';
        const tLbl  = typeLabel[tt] || tt;
        const tClr  = typeColor[tt] || '#4facfe';
        const safeName   = (t.name   || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        const safeChatId = (t.chat_id || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
        return `<div style="display:flex;align-items:center;gap:8px;background:rgba(79,172,254,0.04);border:1px solid rgba(79,172,254,${t.enabled ? '0.2' : '0.08'});border-radius:6px;padding:6px 10px;flex-wrap:wrap;">
            <span style="color:#ccc;font-size:0.8rem;min-width:80px;">${t.name || '未命名'}</span>
            <span style="color:#666;font-size:0.75rem;font-family:monospace;">${t.chat_id}</span>
            <span style="color:${tClr};font-size:0.7rem;background:rgba(79,172,254,0.1);padding:1px 8px;border-radius:10px;">${tLbl}</span>
            <span style="color:${enabledColor};font-size:0.7rem;">${enabledLabel}</span>
            <div style="margin-left:auto;display:flex;gap:5px;">
                <button onclick="testAmplitudeTgTarget(${t.id})" style="width:auto;background:rgba(255,211,51,0.1);color:#ffd233;border:1px solid rgba(255,211,51,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.72rem;">測試</button>
                <button onclick="editAmplitudeTgTarget(${t.id},'${safeName}','${safeChatId}')" style="width:auto;background:rgba(255,159,67,0.1);color:#ff9f43;border:1px solid rgba(255,159,67,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.72rem;">編輯</button>
                <button onclick="toggleAmplitudeTgTargetEnabled(${t.id},${!t.enabled})" style="width:auto;background:rgba(100,100,100,0.1);color:#888;border:1px solid rgba(100,100,100,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.72rem;">${t.enabled ? '停用' : '啟用'}</button>
                <button onclick="deleteAmplitudeTgTarget(${t.id})" style="width:auto;background:rgba(255,68,68,0.1);color:#ff4444;border:1px solid rgba(255,68,68,0.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:0.72rem;">刪除</button>
            </div>
        </div>`;
    }).join('');
}

async function addAmplitudeTgTarget() {
    const nameEl   = document.getElementById('amp-tg-new-name');
    const chatIdEl = document.getElementById('amp-tg-new-chatid');
    const status   = document.getElementById('amp-tg-add-status');
    const name   = nameEl?.value.trim() || '';
    const chatId = chatIdEl?.value.trim() || '';
    if (!name) {
        if (status) { status.style.color = '#f55'; status.textContent = '❌ 名稱不可為空'; }
        return;
    }
    if (!chatId) {
        if (status) { status.style.color = '#f55'; status.textContent = '❌ Telegram Chat ID 不可為空'; }
        return;
    }
    if (status) { status.style.color = '#888'; status.textContent = '新增中...'; }
    try {
        const res  = await fetch('/api/tg/targets', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ chat_id: chatId, name, target_type: 'amplitude', enabled: true }),
        });
        const data = await res.json();
        if (!res.ok) {
            if (status) { status.style.color = '#f55'; status.textContent = `❌ ${data.detail || '新增失敗'}`; }
            return;
        }
        if (status) { status.style.color = '#26de81'; status.textContent = '✅ 新增成功'; }
        if (nameEl)   nameEl.value   = '';
        if (chatIdEl) chatIdEl.value = '';
        loadAmplitudeTgTargets();
        setTimeout(() => { if (status) status.textContent = ''; }, 3000);
    } catch(e) {
        if (status) { status.style.color = '#f55'; status.textContent = `❌ ${e.message}`; }
    }
}

async function editAmplitudeTgTarget(id, currentName, currentChatId) {
    const newName   = prompt('請輸入新的名稱：', currentName);
    if (newName === null) return;
    const newChatId = prompt('請輸入新的 Telegram Chat ID：', currentChatId);
    if (newChatId === null) return;
    if (!newChatId.trim()) { alert('Chat ID 不可為空'); return; }
    try {
        const res  = await fetch(`/api/tg/targets/${id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ name: newName.trim(), chat_id: newChatId.trim() }),
        });
        const data = await res.json();
        if (!res.ok) { alert(`❌ 更新失敗：${data.detail || '未知錯誤'}`); return; }
        loadAmplitudeTgTargets();
    } catch(e) { alert(`❌ 更新失敗：${e.message}`); }
}

async function deleteAmplitudeTgTarget(id) {
    if (!confirm('確定要刪除這個 Telegram 接收對象嗎？')) return;
    try {
        await fetch(`/api/tg/targets/${id}`, { method: 'DELETE' });
        loadAmplitudeTgTargets();
    } catch(e) { alert(`❌ 刪除失敗：${e.message}`); }
}

async function toggleAmplitudeTgTargetEnabled(id, enabled) {
    try {
        await fetch(`/api/tg/targets/${id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ enabled }),
        });
        loadAmplitudeTgTargets();
    } catch(e) { alert(`❌ 更新失敗：${e.message}`); }
}

async function testAmplitudeTgTarget(id) {
    const status = document.getElementById('amp-tg-add-status');
    if (status) { status.style.color = '#888'; status.textContent = '📨 傳送震幅日報中...'; }
    try {
        const res  = await fetch(`/api/amplitude/send_daily_report/${id}`, { method: 'POST' });
        const data = await res.json();
        if (res.ok && data.success) {
            if (status) { status.style.color = '#26de81'; status.textContent = `✅ 已發送（${data.data_date}）`; }
        } else {
            if (status) { status.style.color = '#f55'; status.textContent = `❌ ${data.detail || data.message || '傳送失敗'}`; }
        }
        setTimeout(() => { if (status) status.textContent = ''; }, 5000);
    } catch(e) {
        if (status) { status.style.color = '#f55'; status.textContent = `❌ ${e.message}`; }
    }
}

// -----------------------------------------------------------------------------
// Key broker analysis tab
// -----------------------------------------------------------------------------
let _brokerCurrentCode = '';
let _brokerHasMoneydjData = false;
let _brokerOfficialKeyRows = [];

function _brokerEscape(value) {
    return String(value ?? '').replace(/[&<>'"]/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
    }[ch]));
}

function _brokerFmtNum(value) {
    const n = Number(value || 0);
    return n.toLocaleString();
}

function _brokerPeriodLabel(days, target, completeLabel, partialLabel) {
    const safeDays = Math.max(0, Number(days || 0));
    if (safeDays >= target) return completeLabel;
    return `${partialLabel}（${safeDays}/${target}D）`;
}

function _setBrokerHeader(tbodyId, index, text) {
    const tbody = document.getElementById(tbodyId);
    const headers = tbody?.closest('table')?.querySelectorAll('thead th');
    if (headers && headers[index]) headers[index].textContent = text;
}

function updateBrokerPeriodLabels(data) {
    const s = data.summary || {};
    const days5 = Number(s.available_days_5d || 0);
    const days10 = Number(s.available_days_10d || 0);
    const suffix5 = days5 >= 5 ? '近 5 日' : `目前 ${days5}/5D`;

    _setBrokerHeader('broker-key-tbody', 1, _brokerPeriodLabel(days5, 5, '5D 淨買賣', '區間淨買賣'));
    _setBrokerHeader('broker-key-tbody', 2, _brokerPeriodLabel(days10, 10, '10D 淨買賣', '區間淨買賣'));
    _setBrokerHeader('broker-key-tbody', 3, _brokerPeriodLabel(days5, 5, '5D 買超天數', '買超天數'));
    _setBrokerHeader('broker-top-buy-tbody', 2, _brokerPeriodLabel(days5, 5, '5D 淨買超', '區間淨買超'));
    _setBrokerHeader('broker-top-sell-tbody', 2, _brokerPeriodLabel(days5, 5, '5D 淨賣超', '區間淨賣超'));

    const buyTitle = document.getElementById('broker-top-buy-tbody')?.closest('.broker-section')?.querySelector('.broker-section-title');
    const sellTitle = document.getElementById('broker-top-sell-tbody')?.closest('.broker-section')?.querySelector('.broker-section-title');
    if (buyTitle) buyTitle.textContent = `${suffix5}集中買超`;
    if (sellTitle) sellTitle.textContent = `${suffix5}集中賣超`;
}

function _brokerStatusClass(status) {
    if (['強勢累積', '偏多', '小幅偏多'].includes(status)) return 'positive';
    if (['籌碼轉弱', '分點賣壓'].includes(status)) return 'negative';
    if (status === '無資料') return 'empty';
    return 'neutral';
}

function renderBrokerStockInfo(data) {
    const el = document.getElementById('broker-stock-info');
    if (!el) return;
    const stock = data.stock || {};
    el.innerHTML = `
        <div class="broker-stock-main">
            <span class="broker-stock-code">${_brokerEscape(stock.code || '--')}</span>
            <span class="broker-stock-name">${_brokerEscape(stock.name || '')}</span>
            <span class="broker-stock-category">${_brokerEscape(stock.category || '未分類')}</span>
        </div>
        <div class="broker-stock-date">資料日期：${_brokerEscape(data.data_date || '無分點資料')}</div>
    `;
}

function renderBrokerSummary(data) {
    const el = document.getElementById('broker-summary');
    if (!el) return;
    const s = data.summary || {};
    const status = s.broker_status || '無資料';
    const availableDays5d = Number(s.available_days_5d || 0);
    const availableDays10d = Number(s.available_days_10d || 0);
    const completenessWarning = s.data_completeness_warning || '';
    el.innerHTML = `
        <div class="broker-summary-item">
            <div class="broker-summary-label">資料日期</div>
            <div class="broker-summary-value">${_brokerEscape(data.data_date || '無資料')}</div>
        </div>
        <div class="broker-summary-item">
            <div class="broker-summary-label">已匯入資料天數</div>
            <div class="broker-summary-value">${_brokerEscape(availableDays5d)} / 5D、${_brokerEscape(availableDays10d)} / 10D</div>
        </div>
        <div class="broker-summary-item">
            <div class="broker-summary-label">分點狀態</div>
            <div class="broker-status-badge ${_brokerStatusClass(status)}">${_brokerEscape(status)}</div>
        </div>
        <div class="broker-summary-item">
            <div class="broker-summary-label">5D 分數</div>
            <div class="broker-summary-value">${_brokerEscape(s.broker_score_5d ?? 0)}</div>
        </div>
        <div class="broker-summary-item">
            <div class="broker-summary-label">10D 分數</div>
            <div class="broker-summary-value">${_brokerEscape(s.broker_score_10d ?? 0)}</div>
        </div>
        <div class="broker-summary-item wide">
            <div class="broker-summary-label">主要分點</div>
            <div class="broker-summary-text">${(s.main_key_brokers || []).map(_brokerEscape).join('、') || '尚無'}</div>
        </div>
        <div class="broker-summary-item wide">
            <div class="broker-summary-label">主要警示</div>
            <div class="broker-summary-text ${s.main_warning ? 'warn' : ''}">${_brokerEscape(s.main_warning || '無明顯警示')}</div>
        </div>
        <div class="broker-summary-item wide ${completenessWarning ? 'completeness-warning' : ''}">
            <div class="broker-summary-label">資料完整度</div>
            <div class="broker-summary-text ${completenessWarning ? 'warn' : ''}">${_brokerEscape(completenessWarning || '分點資料天數完整')}</div>
        </div>
        <div class="broker-summary-item wide">
            <div class="broker-summary-label">自動抓取</div>
            <div class="broker-summary-text ${['failed','unsupported','partial'].includes(data.fetch_status) ? 'warn' : ''}">${_brokerEscape(data.fetch_message || '未執行自動抓取')}</div>
        </div>
    `;
}

function _renderBrokerEmpty(tbody, colspan, text) {
    if (tbody) tbody.innerHTML = `<tr><td colspan="${colspan}" class="broker-empty-cell">${_brokerEscape(text)}</td></tr>`;
}

function renderKeyBrokersTable(rows) {
    const tbody = document.getElementById('broker-key-tbody');
    if (!tbody) return;
    _brokerOfficialKeyRows = rows || [];
    if (!_brokerOfficialKeyRows.length) {
        const message = _brokerHasMoneydjData
            ? '目前可使用 MoneyDJ 區間彙總資料分析多日買賣超結構；尚未匯入官方每日 CSV，因此無法判斷逐日連買、轉賣或隔日沖。'
            : '目前沒有官方每日 CSV 逐日分點資料；可先使用上方 MoneyDJ 區間分點資料查詢區間買賣超結構。';
        return _renderBrokerEmpty(tbody, 7, message);
    }
    tbody.innerHTML = _brokerOfficialKeyRows.map(r => `
        <tr>
            <td>${_brokerEscape(r.display_name)}</td>
            <td class="num ${Number(r.net_5d || 0) >= 0 ? 'pos' : 'neg'}">${_brokerFmtNum(r.net_5d)}</td>
            <td class="num ${Number(r.net_10d || 0) >= 0 ? 'pos' : 'neg'}">${_brokerFmtNum(r.net_10d)}</td>
            <td class="num">${_brokerEscape(r.buy_days_5d ?? 0)}</td>
            <td>${_brokerEscape(r.latest_action)}</td>
            <td>${_brokerEscape(r.broker_type)}</td>
            <td>${_brokerEscape(r.judgement)}</td>
        </tr>
    `).join('');
}

function renderTopBuyBrokersTable(rows) {
    const tbody = document.getElementById('broker-top-buy-tbody');
    if (!tbody) return;
    if (!rows || !rows.length) return _renderBrokerEmpty(tbody, 5, '目前沒有集中買超分點');
    tbody.innerHTML = rows.map(r => `
        <tr>
            <td>${r.rank}</td>
            <td>${_brokerEscape(r.display_name)}</td>
            <td class="num pos">${_brokerFmtNum(r.net_5d)}</td>
            <td class="num">${Number(r.volume_ratio_5d || 0).toFixed(2)}%</td>
            <td>${_brokerEscape(r.judgement)}</td>
        </tr>
    `).join('');
}

function renderTopSellBrokersTable(rows) {
    const tbody = document.getElementById('broker-top-sell-tbody');
    if (!tbody) return;
    if (!rows || !rows.length) return _renderBrokerEmpty(tbody, 5, '目前沒有集中賣超分點');
    tbody.innerHTML = rows.map(r => `
        <tr>
            <td>${r.rank}</td>
            <td>${_brokerEscape(r.display_name)}</td>
            <td class="num neg">${_brokerFmtNum(r.net_5d)}</td>
            <td class="num">${Number(r.volume_ratio_5d || 0).toFixed(2)}%</td>
            <td>${_brokerEscape(r.judgement)}</td>
        </tr>
    `).join('');
}

function _renderMoneydjBrokerRows(tbody, rows, side) {
    if (!tbody) return;
    const list = (rows || []).slice(0, 10);
    if (!list.length) {
        return _renderBrokerEmpty(tbody, 6, '目前沒有 MoneyDJ 區間資料');
    }
    tbody.innerHTML = list.map((r, idx) => {
        const net = Math.abs(Number(r.net_lots || 0));
        return `
            <tr>
                <td>${idx + 1}</td>
                <td>${_brokerEscape(r.broker_name)}</td>
                <td class="num">${_brokerFmtNum(r.buy_lots)}</td>
                <td class="num">${_brokerFmtNum(r.sell_lots)}</td>
                <td class="num ${side === 'buy' ? 'pos' : 'neg'}">${_brokerFmtNum(net)}</td>
                <td class="num">${Number(r.volume_ratio || 0).toFixed(2)}%</td>
            </tr>
        `;
    }).join('');
}

function _brokerMoneydjStatusClass(status) {
    if (status === '區間買盤集中') return 'positive';
    if (status === '區間賣壓集中') return 'negative';
    if (status === '多空分歧 / 換手明顯') return 'empty';
    return 'neutral';
}

function renderMoneydjChipSummary(summary) {
    const wrap = document.getElementById('broker-moneydj-chip-summary');
    const statusEl = document.getElementById('broker-moneydj-chip-status');
    const reasonEl = document.getElementById('broker-moneydj-chip-reason');
    if (!wrap || !statusEl || !reasonEl) return;
    const status = summary?.period_chip_status || '區間中性';
    statusEl.className = `broker-status-badge ${_brokerMoneydjStatusClass(status)}`;
    statusEl.textContent = status;
    reasonEl.textContent = summary?.period_chip_reason || '';
    wrap.style.display = 'block';
}

function resetMoneydjBrokerPeriod(message) {
    const statusEl = document.getElementById('broker-moneydj-status');
    const contentEl = document.getElementById('broker-moneydj-content');
    const chipEl = document.getElementById('broker-moneydj-chip-summary');
    const buyTbody = document.getElementById('broker-moneydj-buy-tbody');
    const sellTbody = document.getElementById('broker-moneydj-sell-tbody');
    _brokerHasMoneydjData = false;
    if (statusEl) {
        statusEl.style.display = 'block';
        statusEl.className = 'broker-warning-box muted';
        statusEl.textContent = message || '尚未抓取 MoneyDJ 區間資料。';
    }
    if (contentEl) contentEl.style.display = 'none';
    if (chipEl) chipEl.style.display = 'none';
    if (buyTbody) buyTbody.innerHTML = '';
    if (sellTbody) sellTbody.innerHTML = '';
}

async function fetchMoneydjBrokerPeriod() {
    const code = (_brokerCurrentCode || document.getElementById('broker-query-input')?.value || '').trim();
    const period = document.getElementById('broker-moneydj-period')?.value || '5D';
    const statusEl = document.getElementById('broker-moneydj-status');
    const contentEl = document.getElementById('broker-moneydj-content');
    const btn = document.getElementById('broker-moneydj-fetch-btn');
    if (!code) return resetMoneydjBrokerPeriod('請先查詢股票。');
    if (statusEl) {
        statusEl.style.display = 'block';
        statusEl.className = 'broker-warning-box muted';
        statusEl.textContent = `MoneyDJ ${period} 抓取中...`;
    }
    if (btn) btn.disabled = true;
    try {
        const res = await fetch(`/api/broker/moneydj-fetch?code=${encodeURIComponent(code)}&period=${encodeURIComponent(period)}`);
        const data = await res.json();
        if (!res.ok || data.status !== 'success') {
            throw new Error(data.message || data.trace?.parse_status || `HTTP ${res.status}`);
        }
        const summaryRes = await fetch(`/api/broker/period-summary?code=${encodeURIComponent(code)}&period=${encodeURIComponent(period)}`);
        const summary = await summaryRes.json();
        if (!summaryRes.ok || summary.status !== 'success') {
            throw new Error(summary.message || 'MoneyDJ 區間資料讀取失敗');
        }
        _renderMoneydjBrokerRows(document.getElementById('broker-moneydj-buy-tbody'), summary.buy_rows || [], 'buy');
        _renderMoneydjBrokerRows(document.getElementById('broker-moneydj-sell-tbody'), summary.sell_rows || [], 'sell');
        _brokerHasMoneydjData = Boolean((summary.buy_rows || []).length || (summary.sell_rows || []).length);
        if (_brokerHasMoneydjData) renderKeyBrokersTable(_brokerOfficialKeyRows);
        renderMoneydjChipSummary(summary);
        if (contentEl) contentEl.style.display = 'grid';
        if (statusEl) {
            statusEl.className = 'broker-warning-box info';
            statusEl.textContent = `${summary.period_label || period} ${summary.start_date || '--'} / ${summary.end_date || '--'}，單位：張。此資料為區間彙總，不能判斷逐日轉賣或隔日沖。`;
        }
    } catch (err) {
        if (contentEl) contentEl.style.display = 'none';
        if (statusEl) {
            statusEl.className = 'broker-warning-box warning';
            statusEl.textContent = `MoneyDJ 區間資料抓取失敗：${err.message || err}`;
        }
    } finally {
        if (btn) btn.disabled = false;
    }
}

function renderBrokerWarnings(warnings) {
    const el = document.getElementById('broker-warnings');
    if (!el) return;
    const list = warnings || [];
    if (!list.length) {
        el.innerHTML = '<div class="broker-warning-box muted">目前沒有分點警示。</div>';
        return;
    }
    el.innerHTML = list.map(w => `<div class="broker-warning-box ${_brokerEscape(w.level || 'info')}">${_brokerEscape(w.message || '')}</div>`).join('');
}

function renderBrokerConclusion(text) {
    const el = document.getElementById('broker-conclusion');
    if (el) el.textContent = text || '目前沒有結論。';
}

async function searchKeyBrokers() {
    const input = document.getElementById('broker-query-input');
    const loading = document.getElementById('broker-loading');
    const errorEl = document.getElementById('broker-error');
    const resultEl = document.getElementById('broker-result');
    const query = (input?.value || '').trim();

    if (!query) {
        if (errorEl) { errorEl.style.display = 'block'; errorEl.textContent = '請先輸入股票代號或名稱。'; }
        if (resultEl) resultEl.style.display = 'none';
        return;
    }

    if (loading) loading.style.display = 'flex';
    if (errorEl) { errorEl.style.display = 'none'; errorEl.textContent = ''; }
    if (resultEl) resultEl.style.display = 'none';

    try {
        const res = await fetch(`/api/broker/key-points?query=${encodeURIComponent(query)}`);
        const data = await res.json();
        if (!res.ok || data.status === 'error') {
            throw new Error(data.message || data.detail || `HTTP ${res.status}`);
        }
        _brokerCurrentCode = data.stock?.code || query;
        renderBrokerStockInfo(data);
        renderBrokerSummary(data);
        updateBrokerPeriodLabels(data);
        renderKeyBrokersTable(data.key_brokers || []);
        renderTopBuyBrokersTable(data.top_buy_brokers_5d || []);
        renderTopSellBrokersTable(data.top_sell_brokers_5d || []);
        renderBrokerWarnings(data.warnings || []);
        renderBrokerConclusion(data.summary?.conclusion || '');
        resetMoneydjBrokerPeriod('MoneyDJ 5D 區間資料準備抓取...');
        if (resultEl) resultEl.style.display = 'flex';
        fetchMoneydjBrokerPeriod();
    } catch (err) {
        if (errorEl) { errorEl.style.display = 'block'; errorEl.textContent = err.message || '查詢失敗'; }
    } finally {
        if (loading) loading.style.display = 'none';
    }
}

function initBrokerTabSearch() {
    const input = document.getElementById('broker-query-input');
    if (!input || input.dataset.bound === '1') return;
    input.dataset.bound = '1';
    input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') searchKeyBrokers();
    });
}

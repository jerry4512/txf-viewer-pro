// 系統啟動探針
alert("系統核心啟動中...");

let isDualView = false;
let panes = [];
let globalRefPrice = 0;

// 全域錯誤監控 - 放在最前面
window.onerror = function(msg, url, lineNo, columnNo, error) {
    const errText = '偵測到程式崩潰: ' + msg + '\n行號: ' + lineNo + '\n錯誤內容: ' + (error ? error.stack : '無');
    console.error(errText);
    alert(errText);
    return false;
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
        this.isLoading = false;
        this.oldestTime = null;
        this.isSyncing = false;
        this.highOverlay = null;
        this.lowOverlay = null;
    }

    init() {
        const chartOptions = {
            layout: { textColor: '#d1d4dc', background: { type: 'solid', color: '#000000' }, fontSize: 14 },
            grid: { vertLines: { color: '#333333' }, horzLines: { color: '#333333' } },
            crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
            timeScale: { timeVisible: true, secondsVisible: false },
            priceScale: { autoScale: true, borderVisible: false, alignLabels: true },
            localization: {
                timeFormatter: (ts) => {
                    const d = new Date(ts * 1000);
                    return `${d.getFullYear()}/${(d.getMonth()+1).toString().padStart(2,'0')}/${d.getDate().toString().padStart(2,'0')} ${d.getHours().toString().padStart(2,'0')}:${d.getMinutes().toString().padStart(2,'0')}`;
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

        const macdEl = document.getElementById(`${this.id}-macd-chart`);
        this.macdChart = LightweightCharts.createChart(macdEl, { ...chartOptions, timeScale: { ...chartOptions.timeScale, visible: false } });
        this.macdSeries.line = this.macdChart.addLineSeries({ color: '#F8E71C', lineWidth: 2 });
        this.macdSeries.signal = this.macdChart.addLineSeries({ color: '#00E4FF', lineWidth: 2 });
        this.macdSeries.hist = this.macdChart.addHistogramSeries({ color: '#26a69a', priceFormat: { type: 'volume' } });

        this.highOverlay = document.createElement('div');
        this.highOverlay.className = 'marker-overlay';
        this.highOverlay.style.color = '#FF0000';
        mainEl.appendChild(this.highOverlay);
        this.lowOverlay = document.createElement('div');
        this.lowOverlay.className = 'marker-overlay';
        this.lowOverlay.style.color = '#00FF00';
        mainEl.appendChild(this.lowOverlay);

        this.chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
            if (!range || this.isSyncing) return;
            this.isSyncing = true;
            this.macdChart.timeScale().setVisibleLogicalRange(range);
            this.updateMarkers(range);
            if (range.from < 10 && !this.isLoading && this.oldestTime) this.loadMore();
            this.isSyncing = false;
        });

        this.macdChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
            if (!range || this.isSyncing) return;
            this.isSyncing = true;
            this.chart.timeScale().setVisibleLogicalRange(range);
            this.isSyncing = false;
        });

        this.chart.subscribeCrosshairMove(p => {
            if (p.time && !this.isLoading && this.kbarsCache.length > 0) {
                this.macdChart.setCrosshairPosition(undefined, p.time, this.macdSeries.line);
                const candle = p.seriesData.get(this.candleSeries);
                if (candle) {
                    const m5 = p.seriesData.get(this.maSeries[5]);
                    const m10 = p.seriesData.get(this.maSeries[10]);
                    const m20 = p.seriesData.get(this.maSeries[20]);
                    this.updateLegend(candle, m5?m5.value:undefined, m10?m10.value:undefined, m20?m20.value:undefined);
                }
            } else if (this.kbarsCache.length > 0) {
                this.showLatestLegend();
            }
        });

        this.macdChart.subscribeCrosshairMove(p => {
            if (p.time && !this.isLoading && this.kbarsCache.length > 0) {
                this.chart.setCrosshairPosition(undefined, p.time, this.candleSeries);
            }
        });

        document.getElementById(`${this.id}-period`).addEventListener('change', (e) => {
            this.currentPeriod = e.target.value;
            this.reload();
        });

        [5, 10, 20].forEach(n => {
            document.getElementById(`${this.id}-ma${n}`).addEventListener('change', (e) => {
                this.maSeries[n].applyOptions({ visible: e.target.checked });
            });
        });

        document.getElementById(`${this.id}-macd`).addEventListener('change', (e) => {
            macdEl.style.display = e.target.checked ? 'block' : 'none';
            this.resize();
        });

        this.reload();
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
        this.kbarsCache = [];
        this.oldestTime = null;
        this.candleSeries.setData([]);
        [5,10,20].forEach(n => this.maSeries[n].setData([]));
        this.macdSeries.line.setData([]);
        this.macdSeries.signal.setData([]);
        this.macdSeries.hist.setData([]);
        
        const end = new Date();
        const start = new Date();
        const days = this.currentPeriod === 'D' ? 365 : 30;
        start.setDate(start.getDate() - days);
        await this.fetchData(start.toISOString().split('T')[0], end.toISOString().split('T')[0], true);
    }

    async fetchData(s, e, showLoading = false) {
        this.isLoading = true;
        const overlay = document.getElementById(`${this.id}-loading`);
        if (showLoading) overlay.style.display = 'flex';
        try {
            const res = await fetch(`/api/kbars?start=${s}&end=${e}&period=${this.currentPeriod}`);
            const data = await res.json();
            if (data && data.length > 0) {
                const cleanData = data.map(d => ({ ...d, time: Number(d.time) }));
                const existing = new Set(this.kbarsCache.map(k => k.time));
                const newItems = cleanData.filter(k => !existing.has(k.time));
                this.kbarsCache = [...this.kbarsCache, ...newItems].sort((a,b) => a.time - b.time);
                this.oldestTime = this.kbarsCache[0].time;
                this.render();
                if (showLoading) {
                    this.chart.timeScale().setVisibleLogicalRange({ from: this.kbarsCache.length - 150, to: this.kbarsCache.length - 1 });
                }
            }
        } catch (err) { console.error(err); }
        finally {
            overlay.style.display = 'none';
            this.isLoading = false;
        }
    }

    render() {
        this.candleSeries.setData(this.kbarsCache);
        this.maSeries[5].setData(this.calculateEMA(this.kbarsCache, 5));
        this.maSeries[10].setData(this.calculateEMA(this.kbarsCache, 10));
        this.maSeries[20].setData(this.calculateEMA(this.kbarsCache, 20));
        
        const m = this.calculateMACD(this.kbarsCache);
        this.macdSeries.line.setData(m.macdLine);
        this.macdSeries.signal.setData(m.signalLine);
        this.macdSeries.hist.setData(m.histogram);
        this.updateMarkers(this.chart.timeScale().getVisibleLogicalRange());
    }

    updateLegend(c, m5, m10, m20) {
        const el = document.getElementById(`${this.id}-legend`);
        if (!el) return;
        let text = `開:${c.open} 高:${c.high} 低:${c.low} 收:${c.close}`;
        if (document.getElementById(`${this.id}-ma5`).checked && m5 !== undefined) text += ` <span style="color:#FFFF00">5EMA:${m5.toFixed(0)}</span>`;
        if (document.getElementById(`${this.id}-ma10`).checked && m10 !== undefined) text += ` <span style="color:#00FFFF">10EMA:${m10.toFixed(0)}</span>`;
        if (document.getElementById(`${this.id}-ma20`).checked && m20 !== undefined) text += ` <span style="color:#B200FF">20EMA:${m20.toFixed(0)}</span>`;
        el.innerHTML = text;
    }

    showLatestLegend() {
        if (this.kbarsCache.length === 0) return;
        const last = this.kbarsCache[this.kbarsCache.length - 1];
        const m5 = this.calculateEMA(this.kbarsCache, 5).pop();
        const m10 = this.calculateEMA(this.kbarsCache, 10).pop();
        const m20 = this.calculateEMA(this.kbarsCache, 20).pop();
        this.updateLegend(last, m5?m5.value:undefined, m10?m10.value:undefined, m20?m20.value:undefined);
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
        return { macdLine, signalLine, histogram };
    }

    updateMarkers(range) {
        if (!range || this.kbarsCache.length === 0) return;
        let f = Math.max(0, Math.floor(range.from)), t = Math.min(this.kbarsCache.length - 1, Math.ceil(range.to));
        if (f > t) return;
        let maxK = this.kbarsCache[f], minK = this.kbarsCache[f];
        for (let i = f; i <= t; i++) {
            if (this.kbarsCache[i].high > maxK.high) maxK = this.kbarsCache[i];
            if (this.kbarsCache[i].low < minK.low) minK = this.kbarsCache[i];
        }
        const setPos = (el, k, price, offset) => {
            const x = this.chart.timeScale().timeToCoordinate(k.time), y = this.candleSeries.priceToCoordinate(price);
            if (x !== null && y !== null) {
                el.innerText = price; el.style.display = 'block';
                el.style.left = (x - el.clientWidth / 2) + 'px';
                el.style.top = (y + offset) + 'px';
            } else el.style.display = 'none';
        };
        setPos(this.highOverlay, maxK, maxK.high, -30);
        setPos(this.lowOverlay, minK, minK.low, 10);
    }

    onTick(price, t) {
        let pSec = 60;
        if (this.currentPeriod === '5min') pSec = 300; 
        else if (this.currentPeriod === '15min') pSec = 900;
        else if (this.currentPeriod === '30min') pSec = 1800; 
        else if (this.currentPeriod === '60min') pSec = 3600; 
        else if (this.currentPeriod === 'D') pSec = 86400;
        const curT = Math.floor(t / pSec) * pSec;
        if (this.kbarsCache.length > 0) {
            const last = this.kbarsCache[this.kbarsCache.length - 1];
            if (last.time === curT) {
                last.close = price; last.high = Math.max(last.high, price); last.low = Math.min(last.low, price);
            } else {
                this.kbarsCache.push({ time: curT, open: price, high: price, low: price, close: price, volume: 1 });
            }
            this.render();
        }
    }

    resize() {
        const pane = document.getElementById(this.id);
        const mainEl = document.getElementById(`${this.id}-main`);
        const macdEl = document.getElementById(`${this.id}-macd-chart`);
        if (pane && pane.clientWidth > 0) {
            this.chart.resize(pane.clientWidth, mainEl.clientHeight);
            this.macdChart.resize(pane.clientWidth, macdEl.clientHeight);
        }
    }
}

async function startApp(contractCode) {
    document.getElementById('login-container').style.display = 'none';
    document.getElementById('app-container').style.display = 'flex';
    document.getElementById('contract-name').innerText = `台指期近月 (${contractCode})`;

    panes = [new TradingPane('p1'), new TradingPane('p2')];

    try {
        const res = await fetch('/api/snapshot');
        const snap = await res.json();
        globalRefPrice = snap.reference;
        document.getElementById('g-open').innerText = snap.open;
        document.getElementById('g-high').innerText = snap.high;
        document.getElementById('g-low').innerText = snap.low;
        document.getElementById('g-ref').innerText = globalRefPrice;
        document.getElementById('g-vol').innerText = snap.volume;
        updateQuoteUI(snap.close, globalRefPrice, snap.close - globalRefPrice, ((snap.close - globalRefPrice) / globalRefPrice) * 100);
    } catch (e) { console.error(e); }

    panes[0].init();
    panes[1].init();

    const viewBtn = document.getElementById('view-toggle-btn');
    viewBtn.addEventListener('click', () => {
        isDualView = !isDualView;
        const p2 = document.getElementById('pane-2');
        p2.style.display = isDualView ? 'flex' : 'none';
        viewBtn.innerText = isDualView ? "切換單圖視窗" : "切換雙圖視窗";
        setTimeout(() => panes.forEach(p => p.resize()), 50);
        if (isDualView && panes[1].kbarsCache.length === 0) panes[1].reload();
    });

    connectWebSocket();
}

function updateQuoteUI(p, ref, c, cp) {
    const domP = document.getElementById('q-price');
    const domC = document.getElementById('q-change');
    if (!domP || !domC) return;
    domP.innerText = p.toLocaleString();
    let s = c > 0 ? '▲' : (c < 0 ? '▼' : '-');
    let cl = c > 0 ? 'text-up' : (c < 0 ? 'text-down' : 'text-flat');
    domC.innerText = `${s} ${Math.abs(c).toLocaleString()} (${c > 0 ? '+' : ''}${cp.toFixed(2)}%)`;
    domP.className = `price-huge ${cl}`;
    domC.className = `price-change ${cl}`;
}

async function checkStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        if (data.env) {
            document.getElementById('api_key').value = data.env.api_key;
            document.getElementById('secret_key').value = data.env.secret_key;
            document.getElementById('person_id').value = data.env.person_id;
            document.getElementById('is_simulation').checked = data.env.is_simulation;
            document.getElementById('ca_path').value = data.env.ca_path;
            document.getElementById('ca_passwd').value = data.env.ca_passwd;
        }
        if (data.logged_in) startApp(data.contract);
    } catch (e) { console.error(e); }
}

const loginBtn = document.getElementById('login-btn');
if (loginBtn) {
    loginBtn.addEventListener('click', async () => {
        loginBtn.disabled = true;
        document.getElementById('login-msg').innerText = "連線中...";
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
            if (res.ok) startApp(data.contract);
            else {
                document.getElementById('login-msg').innerText = "登入失敗: " + data.detail;
                loginBtn.disabled = false;
            }
        } catch (e) {
            document.getElementById('login-msg').innerText = "伺服器無回應";
            loginBtn.disabled = false;
        }
    });
}

function connectWebSocket() {
    const ws = new WebSocket(`${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`);
    ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.type === 'tick') {
            const t = msg.data.time, price = msg.data.price;
            if (globalRefPrice > 0) updateQuoteUI(price, globalRefPrice, price - globalRefPrice, ((price - globalRefPrice) / globalRefPrice) * 100);
            const qTime = document.getElementById('q-time');
            if (qTime) qTime.innerText = `最後更新: ${new Date(t*1000).toLocaleTimeString('zh-TW', { hour12: false })}`;
            panes.forEach(p => p.onTick(price, t));
        }
    };
    ws.onclose = () => setTimeout(connectWebSocket, 3000);
}

// 執行
checkStatus();
alert("系統核心啟動成功！");

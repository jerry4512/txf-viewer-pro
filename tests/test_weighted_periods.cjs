const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app_pro.js'), 'utf8');
const begin = source.indexOf('class FreelancerWeightedStocksPane {');
const end = source.indexOf('\nfunction ensureFreelancerWeightedStocks()', begin);
const Pane = vm.runInNewContext(source.slice(begin, end) + '\nFreelancerWeightedStocksPane');
const pane = new Pane();
const start = Date.UTC(2026, 8, 16, 1, 0) / 1000;
const stock = { code: '00981A', reference: 10, bars: [
    { time: start + 60, open: 10, high: 12, low: 9, price: 11, volume: 3 },
    { time: start + 300, open: 11, high: 14, low: 10, price: 13, volume: 7 },
    { time: start + 360, open: 13, high: 15, low: 12, price: 14, volume: 2 },
    { time: start + 270 * 60, open: 14, high: 16, low: 13, price: 15, volume: 5 },
] };
const bars = pane.candleBars(stock, 5);
assert.equal(bars.length, 3);
assert.equal(bars[0].time, start);
assert.equal(bars[0].open, 10);
assert.equal(bars[0].high, 14);
assert.equal(bars[0].low, 9);
assert.equal(bars[0].close, 13);
assert.equal(bars[0].volume, 10);
assert.equal(bars[1].time, start + 300);
assert.equal(pane.candleBars(stock, 60).at(-1).time, start + 240 * 60);
pane.stockPayloads['00981A'] = stock;
pane.cacheRealtimeBar({ code: '00981A', time: start + 299, open: 11, high: 14,
    low: 10, close: 12, volume: 8 }, true);
assert.equal(pane.candleBars(stock, 5)[0].volume, 11); // replacement, not double-counting
pane.cacheRealtimeBar({ code: '00981A', time: start + 86400 + 30, price: 20, reference: 15, tick_volume: 2 }, false);
assert.equal(pane.stockPayloads['00981A'].bars.length, 1); // new day resets history
assert.equal(pane.stockPayloads['00981A'].last, 20);
assert.equal(pane.candleBars({ bars: [{time: start + 60, price: 10}] }, 5).length, 0);
console.log('Weighted chart aggregation and realtime regression checks passed');

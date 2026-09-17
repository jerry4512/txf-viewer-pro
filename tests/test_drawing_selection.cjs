const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app_pro.js'), 'utf8');
const begin = source.indexOf('const FREELANCER_DEFAULT_SYMBOL');
const end = source.indexOf('\nfunction ensureFreelancerChart(', begin);

// 這些圖形只存在於 SVG 覆蓋層，因此用最小替身記錄被 append 的節點屬性。
const appended = [];
const stubElement = () => {
    const attrs = {};
    return {
        attrs,
        setAttribute(name, value) { attrs[name] = String(value); }
    };
};
const context = {
    document: {
        getElementById: () => null,
        querySelectorAll: () => [],
        createElementNS: (_ns, tag) => {
            const element = stubElement();
            element.tag = tag;
            return element;
        }
    },
    localStorage: { getItem: () => null }
};
const Chart = vm.runInNewContext(source.slice(begin, end) + '\nFreelancerKChart', context);

const chart = new Chart();
chart.drawingOverlay = {
    clientWidth: 800,
    clientHeight: 400,
    classList: { toggle() {} },
    getBoundingClientRect: () => ({ left: 0, top: 0 }),
    setAttribute() {},
    appendChild(node) { appended.push(node); }
};
chart.chart = { timeScale: () => ({
    timeToCoordinate: time => time,
    coordinateToTime: x => x,
    getVisibleLogicalRange: () => null
}) };
chart.candleSeries = {
    priceToCoordinate: price => 400 - price,
    coordinateToPrice: y => 400 - y
};
chart.renderCostLineSegments = () => {};

const clickAt = (clientX, clientY) => chart.handleDrawClick({
    clientX,
    clientY,
    preventDefault() {},
    stopPropagation() {},
    target: { closest: () => null }
});

const render = () => { appended.length = 0; chart.renderDrawings(); };
const hits = () => appended.filter(node => 'data-drawing-index' in node.attrs);

chart.drawings = [
    { type: 'horizontal', price: 100 },
    { type: 'trendline', start: { time: 10, price: 100 }, end: { time: 90, price: 150 } },
    { type: 'rect', start: { time: 20, price: 110 }, end: { time: 80, price: 140 } }
];

// 每個圖形都要有可點擊的命中區，索引才對得回 this.drawings。
render();
assert.deepEqual([...new Set(hits().map(node => node.attrs['data-drawing-index']))], ['0', '1', '2']);
assert.ok(hits().every(node => node.attrs['pointer-events'] === 'stroke'));
// 矩形命中區只取邊框，框內才不會吃掉圖表的拖曳事件。
const rectHit = hits().find(node => node.tag === 'rect');
assert.equal(rectHit.attrs.fill, 'none');

// 預覽中的圖形不可被選取，否則索引會指向還不存在的 drawings 項目。
chart.previewDrawing = { type: 'trendline', start: { time: 5, price: 90 }, end: { time: 60, price: 130 }, preview: true };
render();
assert.equal(new Set(hits().map(node => node.attrs['data-drawing-index'])).size, 3);
chart.previewDrawing = null;

// 選取後高亮，並畫出兩端的控制點方塊。
chart.selectDrawing(1);
assert.equal(chart.selectedDrawingIndex, 1);
render();
const handles = appended.filter(node => node.attrs.width === '7' && node.attrs.height === '7');
assert.equal(handles.length, 2);
assert.ok(handles.every(node => node.attrs['pointer-events'] === 'none'));

// 選取不存在的索引時保持原樣，避免 Backspace 誤刪別的圖形。
chart.selectDrawing(9);
assert.equal(chart.selectedDrawingIndex, 1);

// Backspace 刪除選取的圖形，且剩下的圖形重新編號。
assert.equal(chart.deleteSelectedDrawing(), true);
assert.deepEqual(chart.drawings.map(shape => shape.type), ['horizontal', 'rect']);
assert.equal(chart.selectedDrawingIndex, null);
render();
assert.deepEqual([...new Set(hits().map(node => node.attrs['data-drawing-index']))], ['0', '1']);

// 沒有選取時 deleteSelectedDrawing 回傳 false，keydown 才不會攔下瀏覽器的上一頁。
assert.equal(chart.deleteSelectedDrawing(), false);

chart.selectDrawing(0);
chart.clearDrawingSelection();
assert.equal(chart.selectedDrawingIndex, null);
assert.equal(chart.deleteSelectedDrawing(), false);

// 單擊完成的圖形：畫完立刻退回游標，下一次點擊才不會又畫一條。
chart.drawings = [];
chart.setDrawTool('horizontal');
clickAt(120, 300);
assert.equal(chart.drawTool, 'cursor');
assert.equal(chart.drawings.length, 1);
clickAt(140, 250);
assert.equal(chart.drawings.length, 1);

// 兩點完成的圖形：第一點仍留在繪圖模式，第二點才收工退回游標。
chart.setDrawTool('rect');
clickAt(100, 320);
assert.equal(chart.drawTool, 'rect');
assert.equal(chart.drawings.length, 1);
clickAt(200, 260);
assert.equal(chart.drawTool, 'cursor');
assert.equal(chart.drawings.length, 2);
assert.equal(chart.pendingDrawPoint, null);
assert.equal(chart.previewDrawing, null);

// 斐波那契趨勢擴展需要三個錨點，前兩點只畫趨勢線預覽。
chart.drawings = [];
chart.setDrawTool('fibextension');
clickAt(100, 300);
assert.equal(chart.drawings.length, 0);
assert.equal(chart.drawTool, 'fibextension');
clickAt(200, 200);
assert.equal(chart.drawings.length, 0);
clickAt(300, 260);
assert.equal(chart.drawTool, 'cursor');
assert.equal(chart.drawings.length, 1);
assert.equal(chart.drawings[0].points.length, 3);
assert.equal(chart.pendingDrawPoints.length, 0);

// 層級價 = P3 + (P2 - P1) × 比例。此處 P1=100, P2=200, P3=140，區間 +100。
const [p1, p2, p3] = chart.drawings[0].points;
assert.deepEqual([p1.price, p2.price, p3.price], [100, 200, 140]);
chart.selectDrawing(0);
render();
const labels = appended.filter(node => 'data-fibonacci-label' in node.attrs);
assert.deepEqual(
    labels.map(node => node.attrs['data-fibonacci-label']),
    ['0', '0.236', '0.618', '0.786', '1', '1.618', '2', '2.618', '3.14', '3.618']
);
assert.equal(labels[0].textContent, '0 (140)');
assert.equal(labels[4].textContent, '1 (240)');
assert.equal(labels[6].textContent, '2 (340)');

// 水平線只跨在第二、第三個錨點之間（x 200 → 300），不拉到右緣。
const levelLines = appended.filter(node => 'data-fibonacci-level' in node.attrs);
const lineStart = Number(levelLines[0].attrs.x1);
assert.equal(lineStart, 200);
assert.equal(Number(levelLines[0].attrs.x2), 300);
// 擴展的標籤靠左（線的左側），回撤才靠右，兩者不會疊在畫面同一側。
assert.ok(labels.every(node => node.attrs['text-anchor'] === 'end'));
assert.ok(labels.every(node => Number(node.attrs.x) < lineStart));
// 水平線是實線；只有三條錨點趨勢線是虛線。
const dashed = appended.filter(node => node.attrs['stroke-dasharray'] === '5 4');
assert.equal(dashed.length, 2);
assert.ok(
    appended.filter(node => 'data-fibonacci-level' in node.attrs)
        .every(node => !node.attrs['stroke-dasharray'])
);

// 三點都畫完前不得產生層級線，否則預覽會誤導價格。
chart.drawings = [];
chart.setDrawTool('fibextension');
clickAt(100, 300);
chart.handleDrawMove({ clientX: 220, clientY: 210, target: { closest: () => null } });
render();
assert.equal(appended.filter(node => 'data-fibonacci-level' in node.attrs).length, 0);
chart.setDrawTool('cursor');

console.log('Drawing selection and delete regression checks passed');

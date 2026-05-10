async function checkStatus() {
    try {
        const res = await fetch('/api/status');
        const data = await res.json();
        if (data.logged_in) {
            startApp(data.contract);
        } else {
            // 如果後端沒登入，顯示登入介面
            document.getElementById('login-container').style.display = 'block';
            document.getElementById('app-container').style.display = 'none';
            
            // 嘗試讀取本地儲存的金鑰來填充表單 (如果有的話)
            // 這部分邏輯可以由使用者點擊按鈕觸發
        }
    } catch (e) {
        console.error("狀態檢查失敗", e);
        document.getElementById('login-container').style.display = 'block';
    }
}

// 啟動全域 RWD 監聽 (加上延遲確保容器尺寸穩定)
window.addEventListener('resize', () => {
    setTimeout(() => {
        if (panes && panes.length > 0) {
            panes.forEach(p => p.resize());
        }
    }, 100);
});

// 執行初始檢查
checkStatus();

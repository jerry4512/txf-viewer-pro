import os
import shioaji as sj
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("SHIOAJI_API_KEY")
secret_key = os.getenv("SHIOAJI_SECRET_KEY")
print(f"API_KEY 前5碼: {api_key[:5] if api_key else '未找到'}")

api = sj.Shioaji(simulation=False)
api.login(api_key=api_key, secret_key=secret_key)

# 1. 先確認 API 用量
print("=== API 用量 ===")
print(api.usage())

# 2. 確認合約
contract = api.Contracts.Futures.TXF.TXF202605
print(f"\n=== 合約資訊 ===")
print(f"code={contract.code}, delivery_date={contract.delivery_date}")

# 3. 用昨天當作 end（避免夜盤 partition 未完成的問題）
today = date.today()
end_date = today - timedelta(days=2)          # 前天（夜盤中避開未完整 partition）
start_date = end_date - timedelta(days=7)     # 往前 7 天

print(f"\n=== 查詢區間 ===")
print(f"start={start_date}, end={end_date}")

kbars = api.kbars(contract, start=str(start_date), end=str(end_date))
print(f"\n=== 結果 ===")
print(f"ts 數量: {len(kbars.ts)}")
if len(kbars.ts) > 0:
    print(f"第一筆: {kbars.ts[0]}")
    print(f"最後一筆: {kbars.ts[-1]}")
    print("✅ 有歷史資料！")
else:
    print("❌ 無資料")

api.logout()

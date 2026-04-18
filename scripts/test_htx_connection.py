
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
import ccxt

# 增加根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_htx_connection():
    # 加载环境变量
    load_dotenv()
    
    api_key = os.getenv("HTX_API_KEY")
    secret = os.getenv("HTX_SECRET")
    
    print(f"--- HTX 连通性测试 ---")
    print(f"API Key: {api_key[:6]}...{api_key[-4:] if api_key else 'None'}")
    
    if not api_key or not secret:
        print("错误: 未在 .env 中找到 HTX_API_KEY 或 HTX_SECRET")
        return

    # 初始化网关实例 (HTX)
    try:
        exchange = ccxt.htx({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
        })
        
        # 1. 测试行情获取
        print("\n[1] 正在测试行情获取 (BTC/USDT)...")
        ticker = exchange.fetch_ticker('BTC/USDT')
        print(f"成功获取行情: BTC/USDT Last={ticker['last']}")
        
        # 2. 测试 K 线获取 (OHLCV)
        print("\n[2] 正在测试 K 线获取 (1h)...")
        ohlcv = exchange.fetch_ohlcv('BTC/USDT', timeframe='1h', limit=5)
        print(f"成功获取 K 线: {len(ohlcv)} 根")
        
        # 3. 测试账户余额 (私有接口验证 API Key)
        print("\n[3] 正在测试账户余额 (私有接口)...")
        balance = exchange.fetch_balance()
        usdt_balance = balance.get('USDT', {}).get('total', 0)
        print(f"成功获取余额: USDT Total={usdt_balance}")
        
        print("\n✅ HTX API 连通性测试全部通过！")
        
    except Exception as e:
        print(f"\n❌ 测试失败: {str(e)}")

if __name__ == "__main__":
    test_htx_connection()

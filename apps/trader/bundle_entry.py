
import os
import sys
from pathlib import Path

# 将项目根目录集成到路径中进行打包
# PyInstaller 会将依赖项分析出来
current_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(current_dir))

from apps.trader.main import main

if __name__ == "__main__":
    # 强制将环境变量注入，便于打包后的内部调用
    # 实际 TRADING_MODE 会被 Electron 传入的 env 覆盖
    if "TRADING_MODE" not in os.environ:
        os.environ["TRADING_MODE"] = "paper"
        
    main()

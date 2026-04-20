
import os
import sys
from pathlib import Path

# ── PyInstaller SSL 修复 ──────────────────────────────────────
# 企业网络代理(TLS inspection)的根 CA 存在于 Windows 系统信任库
# 但不在 certifi 自带的 cacert.pem 中。requests 库仅使用 certifi
# 导致打包后的 exe 无法通过 SSL 验证访问外部 HTTPS API。
# 修复：将 Windows 系统信任库的 CA 合并到 certifi 的 PEM 文件中，
# 通过 REQUESTS_CA_BUNDLE 环境变量生效。
# ──────────────────────────────────────────────────────────────
def _patch_ssl_for_frozen():
    """合并 Windows 系统信任库与 certifi，修复企业代理 SSL 验证。"""
    if not getattr(sys, 'frozen', False):
        return

    import ssl
    import base64

    try:
        import certifi
        bundled_pem = certifi.where()
    except ImportError:
        return

    if not os.path.exists(bundled_pem):
        return

    try:
        # 读取 PyInstaller 自带的 certifi CA 证书
        with open(bundled_pem, "r", encoding="utf-8") as f:
            bundle_text = f.read()

        # 从 Windows 系统信任库导出所有 CA 证书（DER 格式）
        ctx = ssl.create_default_context()
        der_certs = ctx.get_ca_certs(binary_form=True)
        if not der_certs:
            return

        # 转换 DER → PEM 并追加到 bundle
        extra_pems = []
        for der in der_certs:
            pem = "-----BEGIN CERTIFICATE-----\n"
            pem += base64.encodebytes(der).decode("ascii")
            pem += "-----END CERTIFICATE-----\n"
            extra_pems.append(pem)

        combined = bundle_text.rstrip("\n") + "\n\n" + "\n".join(extra_pems)

        # 直接覆写 _MEIPASS 中的 certifi PEM（requests 库通过
        # certifi.where() 引用此路径，无法被环境变量覆盖）
        with open(bundled_pem, "w", encoding="utf-8") as f:
            f.write(combined)
    except Exception:
        pass  # 静默降级，不阻断启动


_patch_ssl_for_frozen()

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

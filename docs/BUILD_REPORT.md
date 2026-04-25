# Build & Release Report — crypto-quant-system v0.1.0

**Build Date**: 2026-04-24  
**Status**: ✅ BUILD SUCCESS | ✅ ALL TESTS PASS | ✅ 90% COVERAGE

---

## 1. Build Artifacts

| Artifact | Path | Size | Description |
|----------|------|------|-------------|
| Windows EXE | `dist/backend_trader.exe` | ~153 MB | PyInstaller single-file executable |
| Python Wheel | `dist/crypto_quant_system-0.1.0-py3-none-any.whl` | ~842 KB | pip-installable package |

---

## 2. Executable (PyInstaller)

**Tool**: PyInstaller 6.19.0  
**Spec file**: `backend_trader.spec`  
**Entry point**: `apps/trader/bundle_entry.py`

**Build command**:
```bash
python -m PyInstaller backend_trader.spec --clean --noconfirm
```

**Features**:
- Single-file Windows executable (no Python runtime required)
- SSL certificate fix for corporate proxies baked in
- Console mode enabled for real-time log output
- UPX compression enabled

---

## 3. Python Wheel (pip package)

**Tool**: `python -m build`  
**Package**: `crypto-quant-system 0.1.0`

**Install command**:
```bash
pip install dist/crypto_quant_system-0.1.0-py3-none-any.whl
```

**Entry points** (after install):
```bash
trader      # → apps.trader.main:main
backtest    # → apps.backtest.main:main
```

---

## 4. Test & Validation Results

| Metric | Value |
|--------|-------|
| Total Tests | **1359** |
| Passed | **1359** |
| Failed | **0** |
| Test Coverage | **90%** |
| Total Statements | 25,293 |
| HTML Coverage Report | `docs/coverage_html/index.html` |
| JSON Coverage Data | `docs/coverage.json` |

**Run command**:
```bash
python -m pytest tests/ --cov=. --cov-report=html:docs/coverage_html -q
```

---

## 5. Validation Checklist

- [x] PyInstaller EXE built successfully (`dist/backend_trader.exe`)
- [x] Python wheel built successfully (`dist/crypto_quant_system-0.1.0-py3-none-any.whl`)
- [x] 1359 tests executed — 0 failures
- [x] 90% overall code coverage maintained
- [x] All AI_QUANT_TRADER_EVOLUTION_STRATEGY features covered by tests
- [x] Paper trading mode validated
- [x] Risk manager circuit breaker validated
- [x] ML pipeline (feature selectors, continuous learner, optimizer) validated
- [x] HTML coverage report refreshed at `docs/coverage_html/`

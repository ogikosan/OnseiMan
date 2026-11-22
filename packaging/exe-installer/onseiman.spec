# PyInstaller 設定: OnseiMan クライアントを「Python なしで動くフォルダ（exe 入り）」にまとめる。
# 実行: .venv\Scripts\python.exe -m PyInstaller packaging\exe-installer\onseiman.spec --noconfirm
# モデル（約 1GB）は入れない。初回起動時に自動でダウンロードする。
from pathlib import Path
from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH).parent.parent  # packaging/exe-installer -> プロジェクト直下

datas = [(str(ROOT / "client" / "web"), "client/web")]
binaries = []
hiddenimports = ["pystray._win32", "client.watchdog", "client.model_setup", "pycaw.pycaw"]
for pkg in ("sherpa_onnx", "sounddevice", "uiautomation", "comtypes", "pycaw"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [str(ROOT / "client" / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "scipy", "pandas", "IPython", "pytest", "fastapi", "uvicorn"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="OnseiMan",
    console=False,
    icon=str(ROOT / "packaging" / "exe-installer" / "onseiman.ico"),
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="OnseiMan")

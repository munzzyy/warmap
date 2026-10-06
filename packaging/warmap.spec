# PyInstaller recipe for the desktop bundle. Run from the repo root:
#
#     pyinstaller packaging/warmap.spec
#
# One folder per platform under dist/warmap/, with the data, the sample, the
# map and the phone app inside the package exactly where config.py expects
# them. Qt WebEngine's helper process, resources and locales come along via
# PyInstaller's own PySide6 hooks.

import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
PACKAGE = ROOT / "warmap"
VERSION = re.search(r'__version__ = "([^"]+)"', (PACKAGE / "__init__.py").read_text(encoding="utf-8")).group(1)

# Listed by walking the package rather than importing it, so the build does
# not depend on warmap being installed in the environment that runs it.
datas = []
for folder in ("data", "sample", "web", "webapp"):
    for path in sorted((PACKAGE / folder).rglob("*")):
        if path.is_file():
            datas.append((str(path), str(Path("warmap") / path.parent.relative_to(PACKAGE))))

if sys.platform == "win32":
    icon = str(ROOT / "packaging" / "warmap.ico")
elif sys.platform == "darwin":
    icon = str(ROOT / "packaging" / "warmap.icns")
else:
    icon = str(ROOT / "bin" / "warmap.png")

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=["warmap.ui.mainwindow", "warmap.ui.phonedialog"],
    excludes=["tkinter", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQuick3D",
              "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
              "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf",
              "PySide6.QtPdfWidgets", "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtSensors",
              "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtSql", "PySide6.QtTest",
              "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtRemoteObjects", "PySide6.QtScxml",
              "PySide6.QtStateMachine", "PySide6.QtTextToSpeech", "PySide6.QtSpatialAudio",
              "PySide6.QtLocation", "PySide6.QtHttpServer", "PySide6.QtWebSockets", "PySide6.QtWebView",
              "PySide6.QtWebEngineQuick", "PySide6.QtUiTools", "PySide6.QtXml", "PySide6.QtSvgWidgets"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="warmap",
    console=False,
    icon=icon,
)
executables = [exe]

# A windowed Windows program has no console, so its command line output goes
# nowhere. Windows users get a second, console-attached binary for the
# terminal commands; it shares every file in the folder with the first one.
if sys.platform == "win32":
    executables.append(EXE(
        pyz,
        a.scripts,
        exclude_binaries=True,
        name="warmap-cli",
        console=True,
        icon=icon,
    ))

coll = COLLECT(*executables, a.binaries, a.datas, name="warmap")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="warmap.app",
        icon=icon,
        bundle_identifier="dev.munzzyy.warmap",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
        },
    )

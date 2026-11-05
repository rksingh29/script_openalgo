"""Build the Strategy Lab launcher release folder:

    python launcher\\build_exe.py               dist\\StrategyLab\\StrategyLabLauncher.exe + the newest package files
    python launcher\\build_exe.py --wheelhouse  also dist\\StrategyLab\\wheelhouse (every dependency wheel: offline install)

The exe (PyInstaller, one file, no console) holds only the launcher. The package (script_openalgo-<version>-py3-none-any.whl
and .tar.gz from build_all.py) is copied NEXT TO it, so a new version needs no new exe: drop the new file in the folder and
press Update package. Uses its own build venv (launcher\\.build_venv) so the main Python and the Strategy Lab venv stay
untouched. The exe needs no Python to start; it creates the Strategy Lab venv with a Python found on the machine
(py -3 / python, or the one set in its Setup tab).
"""
import glob
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BVENV = os.path.join(HERE, ".build_venv")
BPY = os.path.join(BVENV, "Scripts", "python.exe")
OUT = os.path.join(ROOT, "dist", "StrategyLab")


def sh(*cmd):
    print(">", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def newest(pattern):
    files = sorted(glob.glob(os.path.join(ROOT, "dist", pattern)), key=os.path.getmtime)
    return files[-1] if files else None


def main():
    whl, sdist = newest("script_openalgo-*.whl"), newest("script_openalgo-*.tar.gz")
    if not whl:
        raise SystemExit("no dist\\script_openalgo-*.whl: run python build_all.py first")
    if not os.path.exists(BPY):
        sh(sys.executable, "-m", "venv", BVENV)
    sh(BPY, "-m", "pip", "install", "--upgrade", "pip", "pyinstaller")
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    sh(BPY, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed", "--name", "StrategyLabLauncher",
       "--distpath", OUT, "--workpath", os.path.join(ROOT, "build", "launcher"),
       "--specpath", os.path.join(ROOT, "build", "launcher"), os.path.join(HERE, "strategy_lab_launcher.py"))
    for f in (whl, sdist):
        if f:
            shutil.copy2(f, OUT)
    if "--wheelhouse" in sys.argv:
        wh = os.path.join(OUT, "wheelhouse")
        sh(BPY, "-m", "pip", "download", "--only-binary=:all:", "-d", wh, whl)
        py = f"{sys.version_info[0]}.{sys.version_info[1]}"
        n = len(glob.glob(os.path.join(wh, "*.whl")))
        with open(os.path.join(wh, "bundle.json"), "w", encoding="utf-8") as f:
            json.dump({"python": py, "wheels": n, "package": os.path.basename(whl)}, f)
        print(f"wheelhouse: {n} wheels for Python {py} (offline install on that Python version)")
    old = os.path.join(ROOT, "dist", "StrategyLabLauncher.exe")          # the earlier single-exe build
    if os.path.exists(old):
        os.remove(old)
    print(f"\nrelease folder {OUT}:")
    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        print(f"  {f}  ({os.path.getsize(p) / 1e6:.1f} MB)" if os.path.isfile(p) else f"  {f}\\")


if __name__ == "__main__":
    main()

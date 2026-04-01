"""
Build client_agent.exe using PyInstaller.
Run from the client_agent directory:
    python build_exe.py

Output: dist/client_agent.exe (single file, ~15-20 MB)
"""
import subprocess
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
AGENT_PY = os.path.join(SCRIPT_DIR, "client_agent.py")
DIST_DIR = os.path.join(SCRIPT_DIR, "dist")


def main():
    # Ensure PyInstaller is installed
    try:
        import PyInstaller
        print(f"PyInstaller {PyInstaller.__version__} found.")
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # Build single-file exe
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--name", "client_agent",
        "--distpath", DIST_DIR,
        "--workpath", os.path.join(SCRIPT_DIR, "build"),
        "--specpath", SCRIPT_DIR,
        "--clean",
        "--noconfirm",
        # Hidden imports for FastAPI/uvicorn
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols",
        "--hidden-import", "uvicorn.protocols.http",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan",
        "--hidden-import", "uvicorn.lifespan.on",
        "--hidden-import", "uvicorn.lifespan.off",
        AGENT_PY,
    ]

    print("Building client_agent.exe...")
    print(f"Command: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=SCRIPT_DIR)

    if result.returncode == 0:
        exe_path = os.path.join(DIST_DIR, "client_agent.exe")
        if os.path.exists(exe_path):
            size_mb = os.path.getsize(exe_path) / 1024 / 1024
            print(f"\nBuild successful!")
            print(f"Output: {exe_path}")
            print(f"Size: {size_mb:.1f} MB")
        else:
            print("\nBuild completed but exe not found.")
            sys.exit(1)
    else:
        print(f"\nBuild failed (exit code: {result.returncode})")
        sys.exit(1)


if __name__ == "__main__":
    main()

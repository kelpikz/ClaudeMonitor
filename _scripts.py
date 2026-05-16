import PyInstaller.__main__


def build():
    PyInstaller.__main__.run([
        "--onefile",
        "--windowed",
        "--name", "ClaudeMonitor",
        "main.py",
    ])

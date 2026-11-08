"""打开测试页（仅本地 HTML 文件）。"""
import os
import subprocess

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixture.html")
url = "file:///" + path.replace(os.sep, "/")
print(url)

candidates = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

for exe in candidates:
    if os.path.exists(exe):
        print("使用", exe)
        # 单独开一个窗口，不要占用用户正在用的标签页
        subprocess.Popen([exe, "--new-window", url])
        break
else:
    print("没找到 Chrome/Edge")

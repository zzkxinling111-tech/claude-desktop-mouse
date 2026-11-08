"""Open the test page (local HTML file only)."""
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
        print("using", exe)
        # Open a separate window so the user's current tab is left alone
        subprocess.Popen([exe, "--new-window", url])
        break
else:
    print("Chrome/Edge not found")

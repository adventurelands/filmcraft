#!/usr/bin/env python3
"""In-app checks for storytold/filmcraft#2376 (egui 0.37), the same on Linux, macOS and Windows.

receipt.py <out dir> <name>=<binary> ...   (e.g. main=… pr=…)

Each binary is started with the control channel and driven through it, with real OS key presses
for the clipboard. Checks: panels drawn and a screenshot, a floating panel dragged by its header,
text copy and paste through the system clipboard, screen-reader labels from the platform
accessibility API, and input and screenshots sent to a hidden window. Writes receipt.md (one row
per check, one column per binary) and logs/screenshots to the out dir.
"""
import glob, json, os, platform, re, shutil, socket, subprocess, sys, time

OUT = os.path.abspath(sys.argv[1])
BUILDS = [a.split("=", 1) for a in sys.argv[2:]]
PORT = 9876
OS = platform.system()  # Linux, Darwin, Windows
WAYLAND = OS == "Linux" and bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY")
os.makedirs(OUT, exist_ok=True)


def sh(cmd, timeout=60):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def ctl(method, params=None, timeout=70):
    req = {"id": 1, "method": method}
    if params is not None:
        req["params"] = params
    s = socket.create_connection(("127.0.0.1", PORT), timeout=timeout)
    s.sendall(json.dumps(req).encode() + b"\n")
    buf = b""
    while not buf.endswith(b"\n"):
        c = s.recv(1 << 16)
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf or b"{}")


def timed(method, params=None):
    t0 = time.time()
    try:
        r = ctl(method, params)
    except OSError as e:
        r = {"ok": False, "error": f"no reply: {e}"}
    return r, time.time() - t0


# ------------------------------------------------------------------ OS helpers
def clip_set(text):
    if OS == "Darwin":
        subprocess.run(["pbcopy"], input=text, text=True)
    elif OS == "Windows":
        subprocess.run(["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value '{text}'"])
    else:
        subprocess.run(["xclip", "-selection", "clipboard"], input=text, text=True)


def clip_get():
    if OS == "Darwin":
        return sh("pbpaste")
    if OS == "Windows":
        return sh('powershell -NoProfile -Command "Get-Clipboard"')
    return sh("timeout 3 xclip -selection clipboard -o")


WIN_KEYS = r"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System; using System.Runtime.InteropServices;
public class W { [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
                 [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c); }
'@
$h = [IntPtr]HWND
"""


HWND = {}


def win(pid):
    if pid not in HWND:
        HWND[pid] = sh(f'powershell -NoProfile -Command "(Get-Process -Id {pid}).MainWindowHandle"').strip().splitlines()[-1]
    return WIN_KEYS.replace("HWND", HWND[pid])


def front(pid):
    if OS == "Darwin":
        sh(f"osascript -e 'tell application \"System Events\" to set frontmost of (first process whose unix id is {pid}) to true'")
    elif OS == "Windows":
        ps(win(pid) + "[W]::ShowWindow($h, 9) | Out-Null; [W]::SetForegroundWindow($h) | Out-Null")
    else:
        sh(f"xdotool search --pid {pid} --onlyvisible windowactivate --sync 2>&1 | head -2")
    time.sleep(0.5)


def key(pid, k):
    """A real key press with the platform's command modifier: k in 'a', 'c', 'v'."""
    if OS == "Darwin":
        return sh(f"osascript -e 'tell application \"System Events\" to keystroke \"{k}\" using command down'")
    if OS == "Windows":
        return ps(win(pid) + f"[W]::SetForegroundWindow($h) | Out-Null; [System.Windows.Forms.SendKeys]::SendWait('^{k}')")
    return sh(f"xdotool key --clearmodifiers ctrl+{k}")


def ps(script):
    path = os.path.join(OUT, "_ps.ps1")
    open(path, "w").write(script)
    return sh(f'powershell -NoProfile -ExecutionPolicy Bypass -File "{path}"', timeout=120)


def hide(pid):
    if OS == "Darwin":
        return sh(f"osascript -e 'tell application \"System Events\" to set visible of (first process whose unix id is {pid}) to false'")
    if OS == "Windows":
        return ps(win(pid) + "[W]::ShowWindow($h, 6) | Out-Null")  # SW_MINIMIZE
    return sh(f"for w in $(xdotool search --pid {pid} --onlyvisible); do xdotool windowunmap $w; done 2>&1 | head -2")


def show(pid):
    if OS == "Darwin":
        sh(f"osascript -e 'tell application \"System Events\" to set visible of (first process whose unix id is {pid}) to true'")
    elif OS == "Windows":
        ps(win(pid) + "[W]::ShowWindow($h, 9) | Out-Null")  # SW_RESTORE
    else:
        sh(f"for w in $(xdotool search --pid {pid}); do xdotool windowmap $w windowactivate $w; done 2>/dev/null")
    time.sleep(2)


AX_SWIFT = r"""
import ApplicationServices
import Foundation
let pid = pid_t(CommandLine.arguments[1])!
print("trusted:", AXIsProcessTrusted())
let app = AXUIElementCreateApplication(pid)
AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface" as CFString, kCFBooleanTrue)
var labels: [String] = []
func walk(_ e: AXUIElement, _ depth: Int) {
    if depth > 40 || labels.count > 4000 { return }
    for attr in ["AXTitle", "AXDescription", "AXValue"] {
        var v: CFTypeRef?
        if AXUIElementCopyAttributeValue(e, attr as CFString, &v) == .success, let s = v as? String, !s.isEmpty { labels.append(s) }
    }
    var kids: CFTypeRef?
    if AXUIElementCopyAttributeValue(e, kAXChildrenAttribute as CFString, &kids) == .success, let k = kids as? [AXUIElement] {
        for c in k { walk(c, depth + 1) }
    }
}
for _ in 0..<3 { labels = []; walk(app, 0); if labels.count > 20 { break }; Thread.sleep(forTimeInterval: 2) }
print("labels:", labels.count)
for l in labels.prefix(4000) { print("L:", l.replacingOccurrences(of: "\n", with: " ")) }
"""

UIA_PS = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$p = Get-Process -Id PID
$root = [System.Windows.Automation.AutomationElement]::FromHandle($p.MainWindowHandle)
for ($i = 0; $i -lt 3; $i++) {
  $all = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
  if ($all.Count -gt 20) { break }; Start-Sleep 2
}
"labels: " + $all.Count
foreach ($e in $all) { $n = $e.Current.Name; if ($n) { "L: " + $n } }
"""


def screen_reader(pid):
    if OS == "Darwin":
        path = os.path.join(OUT, "_ax.swift")
        open(path, "w").write(AX_SWIFT)
        out = sh(f"swift '{path}' {pid}", timeout=240)
    elif OS == "Windows":
        out = ps(UIA_PS.replace("PID", str(pid)))
    else:
        return None, "not checked on Linux (AT-SPI)"
    labels = [l[3:] for l in out.splitlines() if l.startswith("L: ")]
    head = "; ".join(l for l in out.splitlines() if not l.startswith("L: "))[:200]
    return labels, head


# ------------------------------------------------------------------ the app
class App:
    def __init__(self, binary, tag):
        self.tag = tag
        self.log = os.path.join(OUT, tag + ".log")
        data = os.path.join(OUT, "data-" + tag)
        shutil.rmtree(data, ignore_errors=True)
        env = dict(os.environ, RUST_LOG="info")
        kw = {"creationflags": 0x00000200} if OS == "Windows" else {"start_new_session": True}
        self.p = subprocess.Popen([binary, "--control", str(PORT), "--data-dir", data, "--demo", "--no-recover"],
                                  stdout=open(self.log, "w"), stderr=subprocess.STDOUT, env=env, **kw)
        for _ in range(240):
            try:
                if ctl("ui.inspect", timeout=3).get("ok") is not None:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("control port did not answer")
        ctl("ui.resize", {"width": 1400, "height": 860})
        time.sleep(3)

    def quit(self):
        try:
            ctl("app.quit", timeout=10)
        except OSError:
            pass
        try:
            self.p.wait(15)
        except subprocess.TimeoutExpired:
            self.p.kill()
        time.sleep(1)


def elements(prefix=""):
    r = ctl("ui.elements", {"prefix": prefix})
    return r.get("result") or []


def center(e):
    x, y, w, h = e["rect"]  # [x, y, width, height]
    return x + w / 2, y + h / 2


def floating():
    ui = ctl("ui.inspect").get("result", {}).get("ui", {})
    return ui.get("floating") or []


def check_panels(app):
    grips = [e["id"] for e in elements("panel.drag.")]
    path = os.path.join(OUT, f"{app.tag}-panels.png")
    r, dt = timed("ui.screenshot", {"path": path})
    size = os.path.getsize(path) if os.path.exists(path) else 0
    ok = len(grips) >= 4 and r.get("ok") and size > 10_000
    return ok, f"{len(grips)} panel headers; screenshot {'ok' if r.get('ok') else r.get('error')} ({size // 1024} KB, {dt:.1f} s)"


def check_float_drag(app):
    r = ctl("engine.execute", {"command": "window.floatPanel", "params": {"panel": "Info"}})
    time.sleep(1)
    before = floating()
    grip = next((e for e in elements("panel.float.") if e["id"].endswith(".move")), None)
    if not before or not grip:
        return False, f"no floating group (floatPanel: {r.get('ok')} {r.get('error', '')})"
    x, y = center(grip)
    d, _ = timed("ui.drag", {"from": {"x": x, "y": y}, "to": {"x": x + 120, "y": y + 80}, "steps": 16})
    time.sleep(1)
    after = floating()
    b = json.dumps(before[0])
    a = json.dumps(after[0]) if after else "none"
    moved = a != b
    return bool(d.get("ok")) and moved, f"drag {'ok' if d.get('ok') else d.get('error')}; group before {b[:90]} after {a[:90]}"


def inspect():
    return ctl("ui.inspect").get("result", {})


def check_clipboard(app):
    if WAYLAND:
        return None, "not checked under weston (no virtual-keyboard protocol for real key presses)"
    ctl("ui.focus", {})  # activate the native window (the app's own request, no OS permission)
    front(app.p.pid)
    token = f"fcpaste{int(time.time()) % 100000}"
    clip_set(token)
    r = ctl("ui.click", {"id": "project.search"})
    time.sleep(0.5)
    focus = (inspect().get("keyboardFocus") or {}).get("elementIds")
    out = [key(app.p.pid, "v")]
    time.sleep(1.0)
    field = inspect().get("ui", {}).get("project_search")
    out.append(key(app.p.pid, "a"))
    time.sleep(0.3)
    clip_set("stale")
    time.sleep(0.3)
    out.append(key(app.p.pid, "c"))
    time.sleep(1.0)
    back = clip_get()
    ok = field == token and back == token
    errs = "; ".join(o for o in out if o)[:160]
    return ok, (f"click {'ok' if r.get('ok') else r.get('error')}, keyboard focus {focus}; system clipboard '{token}', "
                f"after Paste the search field holds {field!r}; after Select All and Copy the clipboard holds {back[:40]!r}") + (f" ({errs})" if errs else "")


def check_screen_reader(app):
    labels, head = screen_reader(app.p.pid)
    if labels is None:
        return None, head
    want = ["Project", "Timeline", "Program"]
    found = [w for w in want if any(w in l for l in labels)]
    sample = ", ".join(sorted(set(labels))[:12])
    return len(found) == len(want), f"{len(labels)} labels; has {found or 'none'} of {want}; {head}; e.g. {sample[:200]}"


def check_hidden(app):
    if WAYLAND:
        return None, "not checked under weston (no way to hide a client from outside)"
    hid = hide(app.p.pid)
    time.sleep(3)
    rows = []
    for m, prm in [("ui.inspect", None), ("ui.click", {"id": "project.search"}), ("ui.screenshot", {"path": os.path.join(OUT, f"{app.tag}-hidden.png")})]:
        r, dt = timed(m, prm)
        rows.append((m, dt, "ok" if r.get("ok") else (r.get("error") or "?")[:70]))
    show(app.p.pid)
    front(app.p.pid)
    r, dt = timed("ui.screenshot", {"path": os.path.join(OUT, f"{app.tag}-shown-again.png")})
    rows.append(("shown again: ui.screenshot", dt, "ok" if r.get("ok") else (r.get("error") or "?")[:70]))
    # answered (either way) well before the old 60 s timeout, and works again once shown
    ok = all(dt < 10 for _, dt, _ in rows) and rows[-1][2] == "ok"
    return ok, "; ".join(f"{m} {dt:.1f} s {res}" for m, dt, res in rows) + (f" (hide: {hid[:60]})" if hid else "")


CHECKS = [
    ("Panels drawn, `ui.screenshot`", check_panels),
    ("Floating panel dragged by its header", check_float_drag),
    ("Text paste and copy, system clipboard, real key presses", check_clipboard),
    ("Screen-reader labels (platform accessibility API)", check_screen_reader),
    ("Hidden window: inspect, click, screenshot; then shown again", check_hidden),
]


def mark(ok):
    return "n/a" if ok is None else ("✅" if ok else "❌")


def main():
    results = {}
    for name, binary in BUILDS:
        app = App(binary, name)
        for title, fn in CHECKS:
            try:
                ok, detail = fn(app)
            except Exception as e:  # a broken check is a failed row, not a lost receipt
                ok, detail = False, f"check failed: {e!r}"
            gone = app.p.poll()
            if gone is not None:
                detail += f" [the app exited with status {gone} during this check]"
                ok = False if ok is not None else ok
            results[(name, title)] = (ok, detail)
            print(f"[{name}] {title}: {mark(ok)} {detail}", flush=True)
        app.quit()
        if OS == "Darwin":
            for f in sorted(glob.glob(os.path.expanduser("~/Library/Logs/DiagnosticReports/*filmcraft*"))):
                shutil.copy(f, os.path.join(OUT, f"{name}-crash-{os.path.basename(f)}"))
        log = open(app.log, errors="replace").read()
        dup = len(re.findall(r"Double use of widget ID|widget id clash|ID clash", log, re.I))
        panics = len(re.findall(r"panicked at", log))
        results[(name, "Log")] = (dup == 0 and panics == 0, f"{dup} widget-ID warnings, {panics} panics")
    titles = [t for t, _ in CHECKS] + ["Log"]
    names = [n for n, _ in BUILDS]
    md = [f"{OS} {platform.release()} ({platform.machine()})", "", "| Check | " + " | ".join(names) + " |", "|---|" + "---|" * len(names)]
    for t in titles:
        md.append(f"| {t} | " + " | ".join(f"{mark(results[(n, t)][0])} {results[(n, t)][1]}".replace("|", "/") for n in names) + " |")
    open(os.path.join(OUT, "receipt.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")
    print("\n".join(md))


main()

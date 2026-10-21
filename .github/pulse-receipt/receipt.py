#!/usr/bin/env python3
"""FilmCraft audio on a real PulseAudio daemon (storytold/filmcraft#2407 receipt).

Runs each scenario against each build, fresh app and fresh preferences each time, and writes a
Markdown table plus recordings and logs. The sound server is the stock PulseAudio of the runner
(no mocks). Outputs are null sinks (recorded through their monitors). For the ALSA fallback, ALSA's default device plays into a
second PulseAudio instance on a socket of its own, which FilmCraft's PulseAudio client does not
use, through the ALSA pulse plugin: sound there came through cpal/ALSA.

usage: receipt.py <out dir> <name>=<binary> [<name>=<binary> ...]
"""
import json, math, os, shutil, signal, socket, statistics, struct, subprocess, sys, threading, time

OUT = sys.argv[1]
BUILDS = [a.split("=", 1) for a in sys.argv[2:]]
HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(OUT, "fixtures")
PORT = 9876
RATE = 48000
TPS = 254_016_000_000  # filmcraft_time::Tick per second
os.makedirs(FIX, exist_ok=True)


def sh(cmd, check=False, quiet=True):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"{cmd}: {r.stderr}")
    return r.stdout.strip()


# ---------------------------------------------------------------- the sound server
ALSA_XDG = "/tmp/fc-alsa-server"
ALSA_SERVER = f"unix:{ALSA_XDG}/pulse/native"
ASOUNDRC = os.path.expanduser("~/.asoundrc")


def alsa_server(on, rate=None):
    """The second server, reached only through ALSA's default device (~/.asoundrc). With `rate`,
    it runs at that sample rate, so ALSA's default device does too."""
    # no D-Bus: the main server needs the session bus name PulseAudio claims
    env = dict(os.environ, XDG_RUNTIME_DIR=ALSA_XDG, DBUS_SESSION_BUS_ADDRESS="disabled:")
    conf = os.path.join(ALSA_XDG + "-config", "pulse")
    os.makedirs(conf, exist_ok=True)
    with open(os.path.join(conf, "daemon.conf"), "w") as f:
        f.write(f"default-sample-rate = {rate}\nalternate-sample-rate = {rate}\n" if rate else "")
    env["XDG_CONFIG_HOME"] = ALSA_XDG + "-config"
    subprocess.run(["pulseaudio", "-k"], env=env, capture_output=True)
    if os.path.exists(ASOUNDRC):
        os.remove(ASOUNDRC)
    # Ubuntu's 99-pulse*.conf points ALSA's default at the main server after ~/.asoundrc is read;
    # set it aside while the second server is in use (pulse.conf, the plugin itself, stays)
    import glob
    for d in ("/usr/share/alsa/alsa.conf.d", "/etc/alsa/conf.d"):
        for c in glob.glob(f"{d}/99-pulse*.conf") if on else glob.glob(f"{d}/99-pulse*.conf.off"):
            sh(f"sudo mv {c} {c + '.off' if on else c[:-4]}")
    if not on:
        return
    os.makedirs(ALSA_XDG, mode=0o700, exist_ok=True)
    subprocess.Popen(["pulseaudio", "-n", "-F", os.path.join(HERE, "server-alsa.pa"), "--exit-idle-time=-1", "--log-target=file:" + os.path.join(OUT, "pulseaudio-alsa.log")],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if os.path.exists(f"{ALSA_XDG}/pulse/native"):
            break
        time.sleep(0.1)
    keep_running("fc_alsa", server=ALSA_SERVER)
    # cpal tries the "pulse" device before "default": both go to this server
    rc = "".join(f'pcm.!{n} {{ type pulse server "{ALSA_SERVER}" }}\nctl.!{n} {{ type pulse server "{ALSA_SERVER}" }}\n' for n in ("default", "pulse"))
    open(ASOUNDRC, "w").write(rc)


def keep_running(sink, server=None, settle=3.0):
    """PulseAudio's null sink renders in 2 s blocks while idle, so a stream on an idle one starts
    about 2 s late (stock pacat plays a 3.0 s file in 5.0 s), which FilmCraft's 1 s stall limit
    reads as a dead output. A silent stream keeps the sink running at low latency, as a real
    output is, without adding to what is recorded."""
    cmd = ["pacat", "--latency-msec=10", "-d", sink, "/dev/zero"]
    if server:
        cmd[1:1] = ["-s", server]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    time.sleep(settle)


SERVER = os.environ.get("FC_SERVER", "pulseaudio")  # or "pipewire" (pipewire + wireplumber + pipewire-pulse)


def pw_sinks(config):
    """The null sinks a .pa config loads, as (name, description, channels, positions)."""
    out = []
    for line in open(os.path.join(HERE, config)):
        if line.startswith("load-module module-null-sink"):
            kv = dict(x.split("=", 1) for x in line.split()[2:] if "=" in x)
            out.append((kv["sink_name"], kv.get("sink_properties", "").replace("device.description=", "") or kv["sink_name"]))
    return out


def pw_pulse_start():
    subprocess.Popen(["pipewire-pulse"], stdout=open(os.path.join(OUT, "pipewire-pulse.log"), "a"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(100):
        if sh("pactl info 2>/dev/null | grep -c 'Server Name'") == "1":
            return
        time.sleep(0.1)
    raise RuntimeError("pipewire-pulse did not start")


def pw_start(config, settle):
    """PipeWire as on a desktop: the daemon and the session manager, with null sinks made in the
    daemon (they outlive a pipewire-pulse restart, as real devices do), and pipewire-pulse."""
    sh("pkill -x pipewire-pulse; pkill -x wireplumber; pkill -x pipewire; sleep 0.5")
    log = open(os.path.join(OUT, "pipewire.log"), "a")
    subprocess.Popen(["pipewire"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(0.5)
    subprocess.Popen(["wireplumber"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    time.sleep(1.0)
    for name, desc in pw_sinks(config):
        sh(f"pw-cli create-node adapter '{{ factory.name=support.null-audio-sink node.name={name} node.description={desc} "
           f"media.class=Audio/Sink object.linger=true audio.channels=2 audio.position=[ FL FR ] }}'")
    pw_pulse_start()
    sinks = pw_sinks(config)
    if sinks:
        sh(f"pactl set-default-sink {sinks[0][0]}")
    time.sleep(settle)


def pw_surround_sink(name, cmap):
    pos = {"front-left": "FL", "front-right": "FR", "front-center": "FC", "lfe": "LFE", "side-left": "SL", "side-right": "SR", "rear-left": "RL", "rear-right": "RR"}
    p = " ".join(pos[c] for c in cmap.split(","))
    sh(f"pw-cli create-node adapter '{{ factory.name=support.null-audio-sink node.name={name} node.description=Surround-{name} "
       f"media.class=Audio/Sink object.linger=true audio.channels={len(cmap.split(','))} audio.position=[ {p} ] }}'")
    time.sleep(1)


def server_restart():
    """`systemctl --user restart pulseaudio` / `systemctl --user restart pipewire-pulse`."""
    if SERVER == "pipewire":
        sh("pkill -x pipewire-pulse; sleep 0.3")
        pw_pulse_start()
    else:
        pa_start("server.pa", settle=0)


def pa_start(config, settle=3.0):
    if SERVER == "pipewire":
        return pw_start(config, settle)
    sh("pulseaudio -k 2>/dev/null; sleep 0.5")
    subprocess.Popen(["pulseaudio", "-n", "-F", os.path.join(HERE, config), "--exit-idle-time=-1", "--log-target=file:" + os.path.join(OUT, "pulseaudio.log"), "-v"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if sh("pactl info 2>/dev/null | grep -c 'Server Name'") == "1":
            for sink in sh("pactl list sinks short").splitlines():
                keep_running(sink.split()[1], settle=0)
            time.sleep(settle)
            return
        time.sleep(0.1)
    raise RuntimeError("pulseaudio did not start")


def pa_kill():
    """Stop the main server (socket and daemon); the ALSA-side one keeps running."""
    sh("pkill -x pipewire-pulse; sleep 0.5" if SERVER == "pipewire" else "pulseaudio -k 2>/dev/null; sleep 0.5")
    sh(f"rm -f {os.environ.get('XDG_RUNTIME_DIR', '/nonexistent')}/pulse/native")


# ---------------------------------------------------------------- recording
class Rec:
    """Records a PulseAudio source (parec) as s16le, noting
    when each chunk arrived, so a sample's wall-clock time can be estimated."""

    def __init__(self, source, channels=2, server=None, channel_map=None):
        self.ch = channels
        cmd = ["parec", "-d", source, "--raw", "--latency-msec=5", f"--rate={RATE}", f"--channels={channels}", "--format=s16le"]
        if channel_map:
            cmd.append(f"--channel-map={channel_map}")
        if server:
            cmd[1:1] = ["-s", server]
        self.p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.buf = bytearray()
        self.arrivals = []  # (wall time, total frames received by then)
        self.t = threading.Thread(target=self._read, daemon=True)
        self.t.start()

    def _read(self):
        stride = 2 * self.ch
        while True:
            d = self.p.stdout.read1(stride * 240) if hasattr(self.p.stdout, "read1") else self.p.stdout.read(stride * 240)
            if not d:
                return
            self.buf += d
            self.arrivals.append((time.time(), len(self.buf) // stride))

    def stop(self):
        self.p.send_signal(signal.SIGINT)
        try:
            self.p.wait(3)
        except subprocess.TimeoutExpired:
            self.p.kill()
        self.t.join(3)
        n = len(self.buf) // 2
        self.x = struct.unpack("<%dh" % n, bytes(self.buf[: n * 2]))
        self.frames = n // self.ch
        return self

    def channel(self, c, t0=0.0, t1=None):
        a = min(self.frames, max(0, int(t0 * RATE)))
        b = self.frames if t1 is None else min(self.frames, max(0, int(t1 * RATE)))
        return [self.x[i * self.ch + c] / 32768 for i in range(a, max(a, b))]

    def rms(self, t0=0.0, t1=None):
        v = [s for c in range(self.ch) for s in self.channel(c, t0, t1)]
        return math.sqrt(sum(s * s for s in v) / len(v)) if v else 0.0

    def wall(self, frame):
        """Estimated wall time of `frame`: the arrival of the chunk holding it, less the frames after it."""
        for t, total in self.arrivals:
            if total > frame:
                return t - (total - frame) / RATE
        return None

    def save(self, name):
        with open(os.path.join(OUT, name + ".raw"), "wb") as f:
            f.write(self.buf)


def goertzel(x, f):
    k = 2 * math.cos(2 * math.pi * f / RATE)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + k * s1 - s2, s1
    return math.sqrt(max(0.0, s1 * s1 + s2 * s2 - k * s1 * s2)) * 2 / max(1, len(x))


def tone_freq(x):
    """Frequency of a steady tone from its rising zero crossings (two-sample interpolated)."""
    ups = [i + x[i] / (x[i] - x[i + 1]) for i in range(len(x) - 1) if x[i] < 0 <= x[i + 1] and x[i] != x[i + 1]]
    return (len(ups) - 1) / ((ups[-1] - ups[0]) / RATE) if len(ups) > 2 else 0.0


# ---------------------------------------------------------------- the app
def ctl(method, params=None, timeout=30):
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


def run(command, params=None):
    return ctl("engine.execute", {"command": command, "params": params or {}})


class App:
    def __init__(self, binary, tag, env=None):
        self.log = os.path.join(OUT, tag + ".log")
        data = os.path.join(OUT, "data-" + tag)
        shutil.rmtree(data, ignore_errors=True)
        env = dict(os.environ, RUST_LOG=os.environ.get("FC_RUST_LOG", "info"), **(env or {}))
        self.p = subprocess.Popen([binary, "--control", str(PORT), "--data-dir", data, "--empty", "--no-recover"], stdout=open(self.log, "w"), stderr=subprocess.STDOUT, env=env, start_new_session=True)
        for _ in range(240):
            try:
                if ctl("ui.inspect", timeout=3).get("ok") is not None:
                    break
            except OSError:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError("control port did not answer")
        time.sleep(1)

    def sequence(self, fixture, surround=False):
        item = run("file.import", {"paths": [os.path.join(FIX, fixture)]})["result"]["items"][0]
        if surround:
            run("file.newSequence", {"name": "s51", "mix": "5.1", "trackType": "5.1", "audio": 1, "fromItem": item})
        else:
            run("file.newSequenceFromClip", {"items": [item]})
        time.sleep(1)

    def play(self):
        ctl("ui.playback", {"action": "play"})

    def stop(self):
        ctl("ui.playback", {"action": "stop"})

    def state(self):
        r = ctl("ui.inspect").get("result", {})
        pb = r.get("playback", {})
        return {"playing": pb.get("playing"), "audioClock": pb.get("audioClock"), "playhead": r.get("playhead", 0) / TPS, "status": r.get("ui", {}).get("status", "")}

    def lines(self, *words):
        try:
            text = open(self.log, errors="replace").read().splitlines()
        except OSError:
            return []
        return [l for l in text if any(w in l.lower() for w in words) and "arboard" not in l]

    def quit(self):
        try:
            ctl("app.quit", timeout=5)
        except OSError:
            pass
        try:
            self.p.wait(5)
        except subprocess.TimeoutExpired:
            os.killpg(self.p.pid, signal.SIGKILL)


def stream_latency():
    """FilmCraft's stream as PulseAudio times it: (buffer latency, sink latency) in ms, the audio
    the server holds for it and the sink's own delay. cpal's stream comes through the ALSA plugin."""
    for block in sh("pactl list sink-inputs").split("Sink Input #")[1:]:
        if 'application.name = "FilmCraft"' in block or "ALSA plug-in [filmcraft" in block:
            get = lambda key: next((l.split(":", 1)[1].strip().split()[0] for l in block.splitlines() if l.strip().startswith(key)), None)
            b, k = get("Buffer Latency"), get("Sink Latency")
            return (int(b) / 1000 if b else None, int(k) / 1000 if k else None)
    return (None, None)


def stream_spec():
    """FilmCraft's playback stream (not the keep-alive ones), as the server reports it."""
    for block in sh("pactl list sink-inputs").split("Sink Input #")[1:]:
        if 'application.name = "FilmCraft"' not in block and "application.name = \"ALSA plug-in [filmcraft" not in block:
            continue
        get = lambda key: next((l.split(":", 1)[1].strip() for l in block.splitlines() if l.strip().startswith(key)), "?")
        return f"{get('Sample Specification')} ({get('Channel Map')})"
    return "no FilmCraft stream"


# ---------------------------------------------------------------- fixtures
def fixtures():
    sh(f"sox -n -r {RATE} -c 2 -b 16 {FIX}/tone-3s.wav synth 3 sine 440 vol 0.5", check=True)
    sh(f"sox -n -r {RATE} -c 2 -b 16 {FIX}/tone-12s.wav synth 12 sine 440 vol 0.5", check=True)
    # a 10 ms 1 kHz burst at 1, 2, ... 9 s
    sh(f"sox -n -r {RATE} -c 1 -b 16 {FIX}/burst.wav synth 0.01 sine 1000 vol 0.9 pad 0 0.99", check=True)
    sh(f"sox -n -r {RATE} -c 1 -b 16 {FIX}/gap.wav trim 0 1", check=True)
    sh(f"sox {FIX}/gap.wav " + " ".join([f"{FIX}/burst.wav"] * 9) + f" {FIX}/clicks-mono.wav", check=True)
    sh(f"sox {FIX}/clicks-mono.wav -c 2 {FIX}/clicks-10s.wav", check=True)
    tones = [300, 400, 500, 80, 700, 900]  # L R C LFE Ls Rs
    for i, f in enumerate(tones):
        sh(f"sox -n -r {RATE} -c 1 -b 16 {FIX}/ch{i}.wav synth 5 sine {f} vol 0.25", check=True)
    sh(f"sox -M " + " ".join(f"{FIX}/ch{i}.wav" for i in range(6)) + f" {FIX}/surround51-5s.wav", check=True)


# ---------------------------------------------------------------- scenarios
# Each returns (summary cell, details).
def sc_play(binary, tag):
    pa_start("server.pa")
    app = App(binary, tag)
    app.sequence("tone-3s.wav")
    rec = Rec("fc_null.monitor")
    time.sleep(0.3)
    app.play()
    time.sleep(1.5)
    spec = stream_spec()
    mid = app.state()
    time.sleep(1.5)
    app.stop()
    time.sleep(0.5)
    rec.stop().save(tag)
    app.quit()
    r = rec.rms(0.5, 3.0)
    f = tone_freq(rec.channel(0, 0.8, 2.8))
    speed = f / 440
    ok = r > 0.1 and mid["audioClock"] and abs(speed - 1) < 0.005
    return f"{'plays' if ok else 'WRONG'}, RMS {r:.2f}, speed {speed:.4f} (440 Hz tone heard at {f:.1f} Hz)", f"stream {spec}; mid-play {mid}"


def sc_scrub(binary, tag):
    pa_start("server.pa")
    app = App(binary, tag)
    run("prefs.set", {"key": "audio.scrubAudio", "value": True})
    app.sequence("tone-3s.wav")
    els = ctl("ui.elements").get("result", [])
    els = els.get("elements", []) if isinstance(els, dict) else els
    bar = next((e for e in els if e.get("id") == "program.scrubBar"), None)
    if not bar:
        app.quit()
        return "no scrub bar found", ""
    rect = bar["rect"]
    x, y, w, h = (rect if isinstance(rect, list) else [rect["x"], rect["y"], rect["w"], rect["h"]])
    rec = Rec("fc_null.monitor")
    time.sleep(0.3)
    t0 = time.time()
    d = ctl("ui.drag", {"from": {"x": x + 4, "y": y + h / 2}, "to": {"x": x + w * 0.8, "y": y + h / 2}, "steps": 60}, timeout=60)
    dt = time.time() - t0
    time.sleep(1.0)
    head = app.state()["playhead"]
    rec.stop().save(tag)
    app.quit()
    r = rec.rms(0.3, 0.3 + dt + 0.3)
    return f"{'sound while scrubbing' if r > 0.01 else 'SILENT'}, RMS {r:.3f}", f"drag {d.get('ok')} over {dt:.1f} s, playhead after {head:.2f} s"


def sc_output(binary, tag):
    pa_start("server.pa")
    app = App(binary, tag)
    run("prefs.set", {"key": "audioHardware.defaultOutput", "value": "Headphones-fc_alt"})
    time.sleep(1)
    app.sequence("tone-3s.wav")
    a, b = Rec("fc_null.monitor"), Rec("fc_alt.monitor")
    time.sleep(0.3)
    app.play()
    time.sleep(3)
    app.stop()
    time.sleep(0.5)
    a.stop().save(tag + "-default")
    b.stop().save(tag + "-chosen")
    app.quit()
    ra, rb = a.rms(0.5, 3.0), b.rms(0.5, 3.0)
    ok = rb > 0.1 and ra < 0.01
    return f"{'on the chosen output' if ok else 'WRONG'}: chosen RMS {rb:.2f}, default {ra:.2f}", ""


def sc_av_seek(binary, tag):
    """Picture vs sound after pauses and seeks: play, pause, seek, play again (twice); the offset of
    each frame step against the clicks heard in the same stretch of playback."""
    pa_start("server.pa")
    app = App(binary, tag)
    ctl("ui.resize", {"width": 800, "height": 500})
    time.sleep(1)
    app.sequence("clicks-10s.wav")
    rec = Rec("fc_null.monitor")
    time.sleep(0.5)
    polls, segments = [], []
    def play_for(seconds):
        start = time.time()
        app.play()
        end = start + seconds
        while time.time() < end:
            t0 = time.time()
            r = ctl("ui.inspect").get("result", {})
            polls.append(((t0 + time.time()) / 2, r.get("playhead", 0) / TPS))
            time.sleep(0.005)
        app.stop()
        segments.append((start, time.time()))
    play_for(2.6)
    for seek in (1.4, 5.4):
        time.sleep(0.7)  # paused
        run("playhead.set", {"seconds": seek})
        time.sleep(0.5)
        play_for(2.8)
    rec.stop().save(tag)
    app.quit()
    onsets, last = [], -RATE
    for i in range(rec.frames):
        if abs(rec.x[i * 2]) > 9800 and i - last > RATE // 2:
            onsets.append(i)
        if abs(rec.x[i * 2]) > 9800:
            last = i
    heard = [rec.wall(i) for i in onsets if rec.wall(i)]
    def head_at(t):  # the playhead reading nearest wall time t
        return min(polls, key=lambda p: abs(p[0] - t))[1] if polls else 0
    results = []
    for k, (s0, s1) in enumerate(segments):
        # clicks heard in this stretch, each at a whole media second: wall = a + media
        clicks = [(round(head_at(w)), w) for w in heard if s0 + 0.2 < w < s1]
        clicks = [(m, w) for m, w in clicks if m >= 1]
        if not clicks:
            results.append((k, None, 0))
            continue
        a = statistics.median(w - m for m, w in clicks)
        offs = [(p1 - (((t0 + t1) / 2) - a)) * 1000 for (t0, p0), (t1, p1) in zip(polls, polls[1:])
                if s0 + 0.25 < t0 and t1 < s1 and p1 > p0 and t1 - t0 < 0.05]
        results.append((k, statistics.median(offs) if offs else None, len(offs)))
    frame = 1000 / 23.976
    ok = all(m is not None and abs(m) < frame for _, m, _ in results)
    cells = ["first play" if k == 0 else f"after pause + seek {k}" for k, _, _ in results]
    return ("within a frame" if ok else "NOT within a frame") + ": " + "; ".join(f"{c} {m:+.0f} ms" if m is not None else f"{c} n/a" for c, (_, m, _) in zip(cells, results)), \
        f"frame steps per stretch {[n for _, _, n in results]}; clicks heard {len(heard)}; frame = {frame:.1f} ms"


OTHER_XDG = "/tmp/fc-other-server"
OTHER_SOCKET = f"{OTHER_XDG}/native"


def other_server(on):
    """A second PulseAudio that is not the session's: its own socket (and TCP on 127.0.0.1:4713)
    and its own sink, fc_other. Sound there came to the server PULSE_SERVER named."""
    env = dict(os.environ, XDG_RUNTIME_DIR=OTHER_XDG, DBUS_SESSION_BUS_ADDRESS="disabled:")
    subprocess.run(["pulseaudio", "-k"], env=env, capture_output=True)
    if not on:
        return
    os.makedirs(OTHER_XDG, mode=0o700, exist_ok=True)
    subprocess.Popen(["pulseaudio", "-n", "-F", os.path.join(HERE, "server-other.pa"), "--exit-idle-time=-1", "--log-target=file:" + os.path.join(OUT, "pulseaudio-other.log")],
                     env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if os.path.exists(OTHER_SOCKET):
            break
        time.sleep(0.1)
    keep_running("fc_other", server=f"unix:{OTHER_SOCKET}")


def pulse_server_scenario(binary, tag, value):
    pa_start("server.pa")
    other_server(True)
    try:
        app = App(binary, tag, env={"PULSE_SERVER": value})
        app.sequence("tone-3s.wav")
        here, there = Rec("fc_null.monitor"), Rec("fc_other.monitor", server=f"unix:{OTHER_SOCKET}")
        time.sleep(0.3)
        app.play()
        time.sleep(1.5)
        spec = sh(f"pactl -s unix:{OTHER_SOCKET} list sink-inputs | grep -E 'application.name' | grep -v pacat | head -2").replace("\n", " ")
        time.sleep(1.5)
        app.stop()
        here.stop(); there.stop()
        log = app.lines("pulse_server", "sound server", "pulse")
        app.quit()
    finally:
        other_server(False)
    rh, rt = here.rms(0.5, 3.0), there.rms(0.5, 3.0)
    ok = rt > 0.1 and rh < 0.01
    return (f"{'on the server PULSE_SERVER names' if ok else 'WRONG SERVER'}: there RMS {rt:.2f}, session server {rh:.2f}; stream there: {spec.strip() or 'none'}",
            " | ".join(l[-140:] for l in log[:3]))


def sc_tcp(binary, tag):
    return pulse_server_scenario(binary, tag, "tcp:127.0.0.1:4713")


def sc_socket(binary, tag):
    return pulse_server_scenario(binary, tag, f"unix:{OTHER_SOCKET}")


def sc_restart(binary, tag):
    pa_start("server.pa")
    app = App(binary, tag)
    app.sequence("tone-12s.wav")
    app.play()
    time.sleep(2)
    before = app.state()
    t_restart = time.time()
    server_restart()
    rec = Rec("fc_null.monitor")
    states = []
    for _ in range(7):
        time.sleep(1)
        states.append(app.state())
    app.stop()
    rec.stop().save(tag)
    app.quit()
    loud = next((i for i in range(rec.frames) if abs(rec.x[i * 2]) > 3000), None)
    back = (rec.wall(loud) - t_restart) if loud is not None and rec.wall(loud) else None
    windows = [rec.rms(s, s + 1) for s in range(3, 6)]
    ok = all(w > 0.1 for w in windows) and states[-1]["audioClock"]
    heads = ", ".join(f"{s['playhead']:.1f}" for s in states)
    return (f"{'recovers' if ok else 'NO SOUND AFTER RESTART'}" + (f", sound back {back:.2f} s after the restart" if back is not None else "") + f"; RMS in seconds 3-6 after it {' / '.join(f'{w:.2f}' for w in windows)}",
            f"before {before}; playhead each second after: {heads}; audioClock {[s['audioClock'] for s in states]}")


def sc_surround(binary, tag, cmap="front-left,front-right,front-center,lfe,side-left,side-right"):
    pa_start("server.pa")
    n = len(cmap.split(","))
    if SERVER == "pipewire":
        pw_surround_sink("fc_sur", cmap)
        mod = None
    else:
        mod = sh(f"pactl load-module module-null-sink sink_name=fc_sur channels={n} channel_map={cmap} sink_properties=device.description=Surround-fc_sur")
    sh("pactl set-default-sink fc_sur")
    keep_running("fc_sur")
    app = App(binary, tag)
    app.sequence("surround51-5s.wav", surround=True)
    rec = Rec("fc_sur.monitor", channels=n, channel_map=cmap)
    time.sleep(0.3)
    app.play()
    time.sleep(1.5)
    spec = stream_spec()
    time.sleep(2.5)
    app.stop()
    rec.stop().save(tag)
    app.quit()
    if mod:
        sh(f"pactl unload-module {mod}")
    names = ["L", "R", "C", "LFE", "Ls", "Rs"]
    tones = [300, 400, 500, 80, 700, 900]
    speakers = []
    for c, pos in enumerate(cmap.split(",")):
        x = rec.channel(c, 1.0, 3.0)
        lv = {nm: goertzel(x, f) for nm, f in zip(names, tones)}
        heard = [nm for nm, a in lv.items() if a > 0.02]
        speakers.append(f"{pos}: {'+'.join(heard) or 'silent'}")
    discrete = all(len(s.split(": ")[1].split("+")) == 1 and s.split(": ")[1] != "silent" for s in speakers[:6])
    speeds = [tone_freq(rec.channel(c, 1.0, 3.0)) / f for c, f in [(0, 300), (2, 500), (5, 900)]]
    at_speed = all(abs(v - 1) < 0.005 for v in speeds)
    return (f"{'each speaker its own channel' if discrete else 'NOT DISCRETE'}, speed {' / '.join(f'{v:.4f}' for v in speeds)} (L / C / Rs)" + ("" if at_speed else " WRONG SPEED")
            + f"; stream {spec}"), "; ".join(speakers)


def sc_av(binary, tag):
    """Playhead (what the picture follows) against when each click is heard."""
    pa_start("server.pa")
    app = App(binary, tag)
    # The playhead a reply carries is from the frame before it, so the reading is up to a frame
    # old: a small window keeps software-rendered frames short.
    ctl("ui.resize", {"width": 800, "height": 500})
    time.sleep(1)
    app.sequence("clicks-10s.wav")
    rec = Rec("fc_null.monitor")
    time.sleep(0.5)
    app.play()
    polls, fps, lat = [], [], []
    t_end = time.time() + 7
    next_lat = time.time() + 1
    while time.time() < t_end:
        if time.time() >= next_lat:
            lat.append(stream_latency())
            next_lat += 0.5
        t0 = time.time()
        r = ctl("ui.inspect").get("result", {})
        polls.append(((t0 + time.time()) / 2, r.get("playhead", 0) / TPS))
        fps.append(r.get("fps", 0))
        time.sleep(0.005)
    app.stop()
    rec.stop().save(tag)
    app.quit()
    # click onsets: a sample over 0.3 after at least 0.5 s of quiet
    onsets, last = [], -RATE
    for i in range(rec.frames):
        if abs(rec.x[i * 2]) > 9800 and i - last > RATE // 2:
            onsets.append(i)
        if abs(rec.x[i * 2]) > 9800:
            last = i
    clicks = [(k + 1, rec.wall(i)) for k, i in enumerate(onsets) if rec.wall(i)]  # (media s, wall time heard)
    if len(clicks) < 3:
        return "no clicks heard", ""
    # When the sound was at media time m: a line through the clicks (wall = a + b * m).
    n = len(clicks)
    mx = sum(m for m, _ in clicks) / n
    my = sum(w for _, w in clicks) / n
    b = sum((m - mx) * (w - my) for m, w in clicks) / sum((m - mx) ** 2 for m, _ in clicks)
    a = my - b * mx
    # The playhead is reported on the frame grid, so use the moments it steps to a new frame: the
    # clock crossed that frame's start between the two readings.
    offsets = []
    for (t0, p0), (t1, p1) in zip(polls, polls[1:]):
        if p1 > p0 and p1 >= clicks[0][0] - 0.5 and t1 - t0 < 0.05:
            heard = ((t0 + t1) / 2 - a) / b
            offsets.append((p1 - heard) * 1000)
    if not offsets:
        return "no frame steps seen", ""
    f = statistics.median([x for x in fps if x]) if any(fps) else 0
    step = statistics.median([t1 - t0 for (t0, _), (t1, _) in zip(polls, polls[1:])]) * 1000
    spread = statistics.quantiles(offsets, n=4) if len(offsets) >= 4 else [min(offsets), 0, max(offsets)]
    return (f"playhead {statistics.median(offsets):+.0f} ms from the sound (median of {len(offsets)} frame steps, middle half {spread[0]:+.0f} to {spread[2]:+.0f}; readings every {step:.0f} ms)",
            f"positive: the playhead (and picture) runs ahead of what is heard; sound clock fit from {n} clicks, rate {b:.4f} s/s; UI at {f:.0f} fps; "
            f"PulseAudio's timing of the stream, buffer / sink latency ms: {', '.join(f'{x}/{y}' for x, y in lat)}")


def loopback_scenario(binary, tag, setup, during=None, rate=None):
    alsa_server(True, rate)
    try:
        setup()
        app = App(binary, tag)
        app.sequence("tone-12s.wav")
        rec = Rec("fc_alsa.monitor", server=ALSA_SERVER)
        time.sleep(0.3)
        app.play()
        if during:
            time.sleep(1.5)
            during()
        time.sleep(4)
        st = app.state()
        app.stop()
        rec.stop().save(tag)
        log = app.lines("pulse", "sound server", "alsa output", "audio output")
        app.quit()
    finally:
        alsa_server(False)
    r = rec.rms(rec.frames / RATE - 2.5, rec.frames / RATE - 0.5)
    status = st["status"] if "audio" in st["status"].lower() else ""
    if rate:
        f = tone_freq(rec.channel(0, rec.frames / RATE - 2.5, rec.frames / RATE - 0.5))
        status = (status + "; " if status else "") + f"440 Hz tone heard at {f:.1f} Hz" + ("" if abs(f / 440 - 1) < 0.005 else " WRONG SPEED")
        for l in app_lines_resampled(tag):
            status += f"; log: {l}"
    return f"{'plays through ALSA' if r > 0.1 else 'SILENT'}, RMS {r:.2f}" + (f"; status: {status}" if status else ""), " | ".join(l[-160:] for l in log[:6])


def app_lines_resampled(tag):
    try:
        return [l.split("] ", 1)[-1][:120] for l in open(os.path.join(OUT, tag + ".log"), errors="replace") if "resampled" in l][:1]
    except OSError:
        return []


def sc_dropped_44k(binary, tag):
    return loopback_scenario(binary, tag, lambda: pa_start("server.pa"), during=pa_kill, rate=44100)


def sc_refused(binary, tag):
    return loopback_scenario(binary, tag, lambda: pa_start("server-no-sinks.pa"))


def sc_dropped(binary, tag):
    return loopback_scenario(binary, tag, lambda: pa_start("server.pa"), during=pa_kill)


def sc_none(binary, tag):
    return loopback_scenario(binary, tag, pa_kill)


def rig_check():
    """Stock client on the same server: a 3.0 s file through pacat, on an idle null sink and on
    one kept running (as every scenario's sinks are)."""
    pa_start("server.pa")
    sh("pkill -f 'pacat --latency-msec=10'")
    time.sleep(6)
    t = time.time()
    sh(f"pacat --latency-msec=64 -d fc_null --file-format=wav {FIX}/tone-3s.wav")
    idle = time.time() - t
    keep_running("fc_null")
    t = time.time()
    sh(f"pacat --latency-msec=64 -d fc_null --file-format=wav {FIX}/tone-3s.wav")
    running = time.time() - t
    # after a restart: when stock pacat, started as soon as the server answers, is first heard
    t = time.time()
    server_restart()
    rec = Rec("fc_null.monitor")
    sh(f"pacat --latency-msec=64 -d fc_null --file-format=wav {FIX}/tone-3s.wav")
    rec.stop()
    loud = next((i for i in range(rec.frames) if abs(rec.x[i * 2]) > 3000), None)
    after = rec.wall(loud) - t if loud is not None and rec.wall(loud) else None
    return idle, running, after


SCENARIOS = [
    ("play", "Playback (3 s, 440 Hz), and its speed", sc_play),
    ("av", "Picture vs sound", sc_av),
    ("avseek", "Picture vs sound after pauses and seeks", sc_av_seek),
    ("scrub", "Scrubbing (audio scrub on, drag the Program scrub bar)", sc_scrub),
    ("output", "Settings output = a non-default sink", sc_output),
    ("restart", "pipewire-pulse restarted 2 s into playback" if SERVER == "pipewire" else "PulseAudio restarted 2 s into playback", sc_restart),
    ("surround", "5.1 sequence on a 5.1 sink", sc_surround),
    ("refused", "Server has no sink, so it refuses the stream", sc_refused),
    ("dropped", "PulseAudio killed mid-play and not restarted", sc_dropped),
    ("dropped44k", "PulseAudio killed mid-play; ALSA's device at 44.1 kHz, the mixer at 48 kHz", sc_dropped_44k),
    ("none", "No sound server at all", sc_none),
    ("socket", "PULSE_SERVER = another server's Unix socket", sc_socket),
    ("tcp", "PULSE_SERVER = a TCP server (tcp:127.0.0.1:4713)", sc_tcp),
]
if os.environ.get("FC_ONLY"):
    SCENARIOS = [x for x in SCENARIOS if x[0] in os.environ["FC_ONLY"].split(",")]
if SERVER == "pipewire":
    # The ALSA-fallback rows use a second PulseAudio behind ALSA's pulse plugin, and the
    # PULSE_SERVER rows a second PulseAudio: neither installs beside pipewire-pulse.
    SCENARIOS = [x for x in SCENARIOS if x[0] not in ("refused", "dropped", "dropped44k", "none", "socket", "tcp")]

if __name__ == "__main__":
    fixtures()
    rig = rig_check()
    info = sh("pactl info | grep -E 'Server Name|Server Version'").replace("\n", ", ")
    rows = {}
    details = []
    for key, title, fn in SCENARIOS:
        for name, binary in BUILDS:
            tag = f"{key}-{name}"
            try:
                cell, more = fn(binary, tag)
            except Exception as e:  # a failed scenario is a result, not a crashed receipt
                cell, more = f"ERROR: {e}", ""
                sh(f"pkill -f -- '--control {PORT}'")
            rows.setdefault(key, {})[name] = cell
            details.append(f"- **{key} / {name}**: {cell}" + (f"<br>{more}" if more else ""))
            print(f"{tag}: {cell} {more}", flush=True)
    pa_start("server.pa")
    md = [f"Server: {info}. Kernel {sh('uname -r')}, {sh('lsb_release -ds')}.",
          f"Rig check, stock `pacat` playing a 3.0 s file: {rig[0]:.2f} s on an idle null sink, {rig[1]:.2f} s on one kept running by a silent stream (every scenario's sinks are kept running that way). "
          + (f"After a server restart, stock `pacat` is first heard {rig[2]:.2f} s after the restart began." if rig[2] is not None else "After a server restart, stock `pacat` was not heard."), "",
          "| Scenario | " + " | ".join(n for n, _ in BUILDS) + " |", "|---|" + "---|" * len(BUILDS)]
    for key, title, _ in SCENARIOS:
        md.append(f"| {title} | " + " | ".join(rows[key].get(n, "") for n, _ in BUILDS) + " |")
    md += ["", "<details><summary>Details</summary>", "", *details, "", "</details>"]
    open(os.path.join(OUT, "receipt.md"), "w").write("\n".join(md) + "\n")
    print("\n".join(md))

#!/usr/bin/env python3
"""Read the anime-waku and animeruka feeds through free Thai VPN Gate servers, alongside watch.py.

Both sites only answer visitors in Thailand, so GitHub's own servers can't read them. This runs in the background while
watch.py does its other checks: it connects to several Thai servers from VPN Gate (a free volunteer network run by the
University of Tsukuba) at once, reads the feeds through the first one that works, saves each to feeds/<site>.xml and
writes feeds/done, which watch.py waits for before it reads them. The tunnels carry only these requests (curl is tied to
the tunnel by name): the runner's own routes and DNS never change, so watch.py, git and LINE are unaffected throughout.
Ordinary requests only (watch.py's usual user agent): this changes where the request comes from, not what it looks like.
It never fails the job: when no Thai server works, watch.py reads the feeds directly and reports the failure as before.
"""
import base64, csv, io, shutil, signal, socket, subprocess, sys, tempfile, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from watch import FEEDS, UA

SERVER_LIST = "http://www.vpngate.net/api/iphone/"  # VPN Gate's live server list (CSV)
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"  # says which country this connection comes from (loc=TH)
OUT = Path(__file__).parent / "feeds"
DONE = OUT / "done"  # written when this has finished, whatever happened: watch.py waits for it (watch.FEED_WAIT)
# Thai servers connecting at once, servers tried in all, seconds each may take to connect, seconds for everything
# (keep under watch.py's FEED_WAIT), seconds to check a server's port before trying it
AT_ONCE, TRIES, CONNECT_WAIT, DEADLINE, PROBE_WAIT = 4, 8, 30, 150, 3
START = time.time()
# VPN Gate's servers use older encryption than Ubuntu's OpenVPN allows by default; these options let it connect.
# --route-nopull: the server's routes (its "send everything through me") are ignored, so only curl uses the tunnel.
OPENVPN_OPTS = ["--data-ciphers", "AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC",
                "--data-ciphers-fallback", "AES-128-CBC", "--tls-cipher", "DEFAULT:@SECLEVEL=0", "--route-nopull",
                "--connect-retry-max", "1", "--connect-timeout", "10", "--verb", "3"]
CONNECTED = "Initialization Sequence Completed"
GAVE_UP = ("SIGUSR1[soft", "SIGTERM", "AUTH_FAILED", "Exiting due to fatal error")  # OpenVPN failed or is starting over
# Install OpenVPN without `apt-get update` (slow on GitHub's runners, which list many extra repositories) unless the
# runner's package list is too old; skip the recommended extras and the manual-page index rebuild, neither is needed.
# rp_filter off: answers arriving through a tunnel would otherwise be dropped, the runner's way out being its own network.
INSTALL = ("sudo sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0; "
           "command -v openvpn >/dev/null && exit 0; sudo rm -f /var/lib/man-db/auto-update; "
           "a='sudo apt-get install -y -qq --no-install-recommends openvpn'; "
           "$a || { sudo apt-get update -qq && $a; }")


def thai_servers():
    """VPN Gate's Thai servers, best score first: [(ip, score, config text)]."""
    req = urllib.request.Request(SERVER_LIST, headers={"User-Agent": UA})
    text = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    lines = [l for l in text.splitlines() if l and not l.startswith("*")]  # "*vpn_servers" and "*" frame the CSV
    lines[0] = lines[0].lstrip("#")  # the header row starts "#HostName,IP,Score,..."
    rows = [r for r in csv.DictReader(io.StringIO("\n".join(lines)))
            if r.get("CountryShort") == "TH" and r.get("OpenVPN_ConfigData_Base64")]
    rows.sort(key=lambda r: int(r.get("Score") or 0), reverse=True)
    return [(r["IP"], r["Score"], base64.b64decode(r["OpenVPN_ConfigData_Base64"]).decode("utf-8", "replace")) for r in rows]


def answers(config):
    """False if the server's OpenVPN port refuses or ignores a TCP connection; True if it accepts, or uses UDP (which
    can't be checked without OpenVPN)."""
    proto, remote = "udp", None
    for line in config.splitlines():
        words = line.split()
        if len(words) >= 2 and words[0] == "proto":
            proto = words[1]
        elif len(words) >= 3 and words[0] == "remote" and remote is None:
            remote = (words[1], int(words[2]))
            if len(words) >= 4:
                proto = words[3]
    if remote is None or not proto.startswith("tcp"):
        return True
    try:
        socket.create_connection(remote, PROBE_WAIT).close()
        return True
    except OSError:
        return False


def curl(url, dev):
    """A page's text through this tunnel only; '' if it fails."""
    r = subprocess.run(["curl", "-s", "--max-time", "20", "--noproxy", "*", "--interface", f"if!{dev}", "-A", UA, url],
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


class Tunnel:
    """One OpenVPN connection to one server, on its own tun device."""

    def __init__(self, n, ip, config, tmp):
        self.ip, self.dev = ip, f"tun{n}"
        cfg, auth, self.log, self.pid = (tmp / f"vpn{n}.{x}" for x in ("ovpn", "auth", "log", "pid"))
        cfg.write_text(config)
        auth.write_text("vpn\nvpn\n")  # VPN Gate's public login
        self.log.write_text("")
        self.until = min(time.time() + CONNECT_WAIT, START + DEADLINE)
        self.proc = subprocess.Popen(["sudo", "openvpn", "--config", str(cfg), "--auth-user-pass", str(auth), *OPENVPN_OPTS,
                                      "--dev", self.dev, "--writepid", str(self.pid), "--log", str(self.log)],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def state(self):
        """'up', 'waiting', or why it failed."""
        text = self.log.read_text(errors="replace")
        if CONNECTED in text:
            return "up"
        gave_up = next((l for l in text.splitlines() if any(s in l for s in GAVE_UP)), None)
        if gave_up:
            return gave_up.strip()[-120:]
        if self.proc.poll() is not None:
            return "OpenVPN stopped"
        return "waiting" if time.time() < self.until else f"not connected after {CONNECT_WAIT}s"

    def stop(self):
        try:
            subprocess.run(["sudo", "kill", self.pid.read_text().strip()], capture_output=True)
        except OSError:  # no pid file yet
            subprocess.run(["sudo", "pkill", "-f", "--", f"--dev {self.dev} "], capture_output=True)


def read_through(tunnel):
    """Save the feeds if this tunnel comes out in Thailand. The site names saved."""
    # ask where the connection comes from and fetch both feeds at once; the feeds are kept only if it's Thailand
    with ThreadPoolExecutor(len(FEEDS) + 1) as pool:
        trace = pool.submit(curl, TRACE, tunnel.dev)
        pages = {site: pool.submit(curl, url, tunnel.dev) for site, url in FEEDS.items()}
        loc = next((l[4:] for l in trace.result().splitlines() if l.startswith("loc=")), "?")
        pages = {site: page.result() for site, page in pages.items()}
    if loc != "TH":
        print(f"VPN: {tunnel.ip} connected but exits in {loc}, not Thailand")
        return []
    saved = []
    for site, page in pages.items():
        if "<rss" in page[:1000]:
            (OUT / f"{site}.xml").write_text(page, encoding="utf-8")
            saved.append(site)
        else:
            print(f"VPN: {site} didn't send its feed through {tunnel.ip}: {' '.join(page[:120].split())!r}")
    return saved


def connect_and_read(servers):
    """Connect to up to AT_ONCE servers at a time, best first; read the feeds through the first Thai one that works."""
    queue, live = list(enumerate(servers[:TRIES])), []
    with tempfile.TemporaryDirectory() as d:
        try:
            while queue or live:
                while queue and len(live) < AT_ONCE and time.time() < START + DEADLINE - 5:
                    n, (ip, score, config) = queue.pop(0)
                    print(f"VPN: trying Thai server {ip} (score {score})")
                    live.append(Tunnel(n, ip, config, Path(d)))
                if not live:
                    break
                for t in list(live):
                    state = t.state()
                    if state == "waiting":
                        continue
                    live.remove(t)
                    if state == "up":
                        saved = read_through(t)
                        if saved:
                            print(f"VPN: Thai server {t.ip} ok after {time.time() - START:.0f}s, saved {', '.join(saved)}")
                            return True
                    else:
                        print(f"VPN: {t.ip} didn't connect: {state}")
                    t.stop()
                time.sleep(0.2)
            return False
        finally:
            subprocess.run(["sudo", "pkill", "-x", "openvpn"], capture_output=True)


def main():
    OUT.mkdir(exist_ok=True)
    for f in [*OUT.glob("*.xml"), DONE]:
        f.unlink(missing_ok=True)
    install = subprocess.Popen(["bash", "-c", INSTALL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        servers = thai_servers()
        if not servers:
            print("VPN: no Thai servers in VPN Gate's list right now")
            return
        with ThreadPoolExecutor(len(servers)) as pool:
            up = list(pool.map(answers, (config for _, _, config in servers)))
        if not all(up):
            print(f"VPN: {up.count(False)} of {len(servers)} Thai servers don't answer, skipped")
        servers = [s for s, ok in zip(servers, up) if ok]
        try:
            install.wait(timeout=max(1, START + DEADLINE - CONNECT_WAIT - time.time()))
        except subprocess.TimeoutExpired:
            pass  # reported below as not installed
    finally:
        if install.poll() is None:
            install.kill()
    if not shutil.which("openvpn"):
        print("VPN: couldn't install OpenVPN; watch.py reads the feeds directly")
        return
    print(f"VPN: ready after {time.time() - START:.0f}s")
    if not connect_and_read(servers):
        print("VPN: no Thai server worked; watch.py reads the feeds directly")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # stopped from outside: still close the tunnels and write DONE
    try:
        main()
    except Exception as e:  # never fail the job: watch.py carries on and reports the sites it couldn't read
        print(f"VPN: failed: {type(e).__name__}: {e}")
    finally:
        OUT.mkdir(exist_ok=True)
        DONE.touch()
    sys.exit(0)

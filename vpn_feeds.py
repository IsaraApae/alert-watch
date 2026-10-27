#!/usr/bin/env python3
"""Read the animeruka feed through free Thai VPN Gate servers, alongside watch.py.

animeruka only answers visitors in Thailand, so GitHub's own servers can't read it. This runs in the background while
watch.py does its other checks: it connects to the server that worked last time straight away, and to every usable
Thai server from VPN Gate (a free volunteer network run by the University of Tsukuba) at once as soon as VPN Gate's list
comes. It reads the feed through each one as soon as it's up, saves the first good copy to feeds/animeruka.xml and
writes feeds/done, which watch.py waits for before it reads it. The tunnels carry only these
requests (curl is tied to the tunnel by name): the runner's own routes and DNS never change, so watch.py, git and LINE
are unaffected throughout. Ordinary requests only (watch.py's usual user agent): this changes where the request comes
from, not what it looks like. It never fails the job: when no Thai server works, watch.py reads the feed directly and
reports the failure as before.
"""
import base64, csv, io, shlex, shutil, signal, socket, subprocess, sys, tempfile, threading, time, urllib.request
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from watch import FEEDS, UA

SITE = "animeruka"
URL = FEEDS[SITE]
SERVER_LIST = "http://www.vpngate.net/api/iphone/"  # VPN Gate's live server list (CSV)
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"  # says which country this connection comes from (loc=TH)
OUT = Path(__file__).parent / "feeds"
DONE = OUT / "done"  # written when this has finished, whatever happened: watch.py waits for it (watch.FEED_WAIT)
# servers tried (all at once), seconds each may take to connect, seconds for everything (keep under watch.py's
# FEED_WAIT), seconds to check a server's port before trying it, seconds curl may take to read the feed
TRIES, CONNECT_WAIT, DEADLINE, PROBE_WAIT, READ_WAIT = 10, 30, 150, 3, 15
START = time.time()
# VPN Gate's servers use older encryption than Ubuntu's OpenVPN allows by default; these options let it connect.
# --route-nopull: the server's routes (its "send everything through me") are ignored, so only curl uses the tunnel.
OPENVPN_OPTS = ["--data-ciphers", "AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC",
                "--data-ciphers-fallback", "AES-128-CBC", "--tls-cipher", "DEFAULT:@SECLEVEL=0", "--route-nopull",
                "--connect-retry-max", "1", "--connect-timeout", "10", "--verb", "3"]
CONNECTED = "Initialization Sequence Completed"
GAVE_UP = ("SIGUSR1[soft", "SIGTERM", "AUTH_FAILED", "Exiting due to fatal error")  # OpenVPN failed or is starting over
DEBS = Path(__file__).parent / "debs"  # the OpenVPN packages, kept between runs by the workflow's cache step
LAST = Path(__file__).parent / "vpn_last.ovpn"  # the last server that worked, tried first (committed with the state)
# Install OpenVPN from the packages kept from an earlier run: no package lists to read, nothing to download. Without
# them (the first run, or the runner changed Ubuntu version), apt downloads them into debs/ for the cache to keep,
# without `apt-get update` (slow on GitHub's runners, which list many extra repositories) unless the runner's package
# list is too old. Either way: skip the recommended extras and the manual-page index rebuild, and don't wait for each
# file to reach the disk (--force-unsafe-io; the runner is thrown away after the job). If the kept packages no longer
# fit the runner, apt -f fills in what's missing. lock and partial/ are apt's own, and the cache can't read them.
# rp_filter off: answers arriving through a tunnel would otherwise be dropped, the runner's way out being its own network.
INSTALL = ("sudo sysctl -qw net.ipv4.conf.all.rp_filter=0 net.ipv4.conf.default.rp_filter=0; "
           "command -v openvpn >/dev/null && exit 0; sudo rm -f /var/lib/man-db/auto-update; "
           f"d={shlex.quote(str(DEBS))}; "
           "a() { sudo apt-get install -y -qq --no-install-recommends -o Dpkg::Options::=--force-unsafe-io \"$@\"; }; "
           "if ls \"$d\"/*.deb >/dev/null 2>&1; then sudo dpkg --force-unsafe-io -i \"$d\"/*.deb >/dev/null || a -f; "
           "command -v openvpn >/dev/null && exit 0; fi; "
           "sudo rm -rf \"$d\"; mkdir -p \"$d/partial\"; o=\"-oDir::Cache::Archives=$d/\"; "
           "a \"$o\" openvpn || { sudo apt-get update -qq && a \"$o\" openvpn; }; sudo rm -rf \"$d/partial\" \"$d/lock\"")


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
    """A page's text through this tunnel only; '' if it fails. --compressed: the feed comes gzipped, much smaller."""
    r = subprocess.run(["curl", "-s", "--compressed", "--connect-timeout", "5", "--max-time", str(READ_WAIT),
                        "--noproxy", "*", "--interface", f"if!{dev}", "-A", UA, url], capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


class Tunnel:
    """One OpenVPN connection to one server, on its own tun device."""

    def __init__(self, n, ip, config, tmp):
        self.ip, self.dev, self.config = ip, f"tun{n}", config
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
    """The feed through this tunnel; '' if the site didn't send it (and says why). Getting the feed at all proves the
    tunnel comes out in Thailand, so where it comes out is only asked when it fails."""
    page = curl(URL, tunnel.dev)
    if "<rss" in page[:1000]:
        return page
    loc = next((l[4:] for l in curl(TRACE, tunnel.dev).splitlines() if l.startswith("loc=")), "?")
    why = f"it exits in {loc}, not Thailand" if loc != "TH" else repr(" ".join(page[:120].split()))
    print(f"VPN: {SITE} didn't send its feed through {tunnel.ip}: {why}")
    return ""


def usable_servers():
    """VPN Gate's Thai servers that answer, best first: [(ip, score, config text)]."""
    servers = thai_servers()
    if not servers:
        print("VPN: no Thai servers in VPN Gate's list right now")
        return []
    with ThreadPoolExecutor(len(servers)) as pool:
        up = list(pool.map(answers, (config for _, _, config in servers)))
    if not all(up):
        print(f"VPN: {up.count(False)} of {len(servers)} Thai servers don't answer, skipped")
    return [s for s, ok in zip(servers, up) if ok]


def last_server():
    """The server that read the feed last time, as (ip, None, config text), or None. vpn_last.ovpn is its OpenVPN
    config under two comment lines, the first just its IP."""
    try:
        ip, _, config = LAST.read_text().split("\n", 2)
    except (OSError, ValueError):
        return None
    return (ip[2:].strip(), None, config) if ip.startswith("# ") and config.strip() else None


def connect_and_read(first, listed):
    """Connect to the last server that worked straight away, and to the others as soon as VPN Gate's list comes
    (listed, a Future), all at once. Read the feed through each as soon as it's up, in the background so a slow one
    holds up no other. The first copy of the feed wins, and its server is kept for next time."""
    with tempfile.TemporaryDirectory() as d, ThreadPoolExecutor(TRIES) as pool:
        try:
            live, reading, tried = [], {}, set()  # tunnels connecting; {feed being read: its tunnel}; IPs tried

            def start(servers):
                for ip, score, config in servers:
                    if ip not in tried and len(tried) < TRIES and time.time() < START + DEADLINE - 5:
                        print(f"VPN: trying Thai server {ip} ({f'score {score}' if score else 'worked last time'})")
                        live.append(Tunnel(len(tried), ip, config, Path(d)))
                        tried.add(ip)

            start([first] if first else [])
            have_list = False
            while (live or reading or not have_list) and time.time() < START + DEADLINE:
                if not have_list and listed.done():
                    have_list = True
                    try:
                        start(listed.result())
                    except Exception as e:
                        print(f"VPN: couldn't read VPN Gate's list: {type(e).__name__}: {e}")
                for t in list(live):
                    state = t.state()
                    if state == "waiting":
                        continue
                    live.remove(t)
                    if state == "up":
                        reading[pool.submit(read_through, t)] = t
                    else:
                        print(f"VPN: {t.ip} didn't connect: {state}")
                        t.stop()
                for f in [f for f in reading if f.done()]:
                    t = reading.pop(f)
                    page = f.result() if not f.exception() else ""
                    if page:
                        (OUT / f"{SITE}.xml").write_text(page, encoding="utf-8")
                        print(f"VPN: Thai server {t.ip} ok after {time.time() - START:.0f}s, saved {SITE}")
                        if not first or t.ip != first[0]:
                            LAST.write_text(f"# {t.ip}\n# the VPN Gate server that last read the {SITE} feed: "
                                            f"vpn_feeds.py tries it first\n{t.config}")
                        return True
                    t.stop()
                time.sleep(0.2)
            return False
        finally:  # before the pool closes: reads still going through the other tunnels then end at once
            subprocess.run(["sudo", "pkill", "-x", "openvpn"], capture_output=True)


def main():
    OUT.mkdir(exist_ok=True)
    for f in [*OUT.glob("*.xml"), DONE]:
        f.unlink(missing_ok=True)
    install = subprocess.Popen(["bash", "-c", INSTALL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # VPN Gate's list is read in the background meanwhile: the last server that worked needn't wait for it. A thread of
    # its own (daemon) so that a slow list never keeps this from finishing once the feed is in.
    listed = Future()

    def read_list():
        try:
            listed.set_result(usable_servers())
        except Exception as e:
            listed.set_exception(e)

    threading.Thread(target=read_list, daemon=True).start()
    try:
        install.wait(timeout=max(1, START + DEADLINE - CONNECT_WAIT - time.time()))
    except subprocess.TimeoutExpired:
        install.kill()  # reported below as not installed
    if not shutil.which("openvpn"):
        print("VPN: couldn't install OpenVPN; watch.py reads the feed directly")
        return
    print(f"VPN: ready after {time.time() - START:.0f}s")
    if not connect_and_read(last_server(), listed):
        print("VPN: no Thai server worked; watch.py reads the feed directly")


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

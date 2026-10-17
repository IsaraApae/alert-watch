#!/usr/bin/env python3
"""Read the anime-waku and animeruka feeds through a free Thai VPN Gate server, then disconnect.

Both sites only answer visitors in Thailand, so GitHub's own servers can't read them. This runs as its own workflow
step before watch.py: it connects to one Thai server from VPN Gate (a free volunteer network run by the University of
Tsukuba), saves each feed to feeds/<site>.xml, and drops the VPN, so watch.py, git and LINE use GitHub's normal network.
Ordinary requests only (watch.py's usual user agent): this changes where the request comes from, not what it looks like.
It never fails the job: when no Thai server works, watch.py reads the feeds directly and reports the failure as before.
"""
import base64, csv, io, os, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from urllib.parse import urlparse

from watch import FEEDS, UA

SERVER_LIST = "http://www.vpngate.net/api/iphone/"  # VPN Gate's live server list (CSV)
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"  # says which country this connection comes from (loc=TH)
OUT = Path(__file__).parent / "feeds"
TRIES, CONNECT_WAIT, DEADLINE = 4, 30, 170  # servers to try, seconds to wait for each, seconds for the whole step
START = time.time()
# VPN Gate's servers use older encryption than Ubuntu's OpenVPN allows by default; these options let it connect
OPENVPN_OPTS = ["--data-ciphers", "AES-256-GCM:AES-128-GCM:CHACHA20-POLY1305:AES-128-CBC",
                "--data-ciphers-fallback", "AES-128-CBC", "--tls-cipher", "DEFAULT:@SECLEVEL=0",
                "--redirect-gateway", "def1", "--connect-retry-max", "1", "--verb", "3"]


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


def resolve(urls):
    """Look up each host before connecting: GitHub's DNS server may be unreachable once traffic goes through the VPN."""
    out = []
    for url in urls:
        host = urlparse(url).hostname
        out += ["--resolve", f"{host}:443:{socket.gethostbyname(host)}"]
    return out


def curl(url, pins):
    """A page's text through whatever network is up now; '' if it fails."""
    r = subprocess.run(["curl", "-s", "--max-time", "20", "-A", UA, *pins, url], capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def stop_vpn(proc):
    subprocess.run(["sudo", "pkill", "-x", "openvpn"], capture_output=True)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        subprocess.run(["sudo", "pkill", "-9", "-x", "openvpn"], capture_output=True)
    time.sleep(1)  # let OpenVPN remove its routes


def try_server(ip, config, pins, tmp):
    """Connect to one server; save the feeds if the connection is Thai. The site names saved."""
    cfg, auth, log = tmp / "vpn.ovpn", tmp / "auth.txt", tmp / "vpn.log"
    cfg.write_text(config)
    auth.write_text("vpn\nvpn\n")  # VPN Gate's public login
    log.write_text("")
    proc = subprocess.Popen(["sudo", "openvpn", "--config", str(cfg), "--auth-user-pass", str(auth), *OPENVPN_OPTS,
                             "--log", str(log)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_until = min(time.time() + CONNECT_WAIT, START + DEADLINE)
        while time.time() < wait_until and proc.poll() is None:
            if "Initialization Sequence Completed" in log.read_text(errors="replace"):
                break
            time.sleep(1)
        else:
            print(f"VPN: {ip} didn't connect")
            return []
        loc = next((l[4:] for l in curl(TRACE, pins).splitlines() if l.startswith("loc=")), "?")
        if loc != "TH":
            print(f"VPN: {ip} connected but exits in {loc}, not Thailand")
            return []
        saved = []
        for site, url in FEEDS.items():
            page = curl(url, pins)
            if "<rss" in page[:1000]:
                (OUT / f"{site}.xml").write_text(page, encoding="utf-8")
                saved.append(site)
            else:
                print(f"VPN: {site} didn't send its feed through {ip}: {' '.join(page[:120].split())!r}")
        return saved
    finally:
        stop_vpn(proc)


def main():
    OUT.mkdir(exist_ok=True)
    for f in OUT.glob("*.xml"):
        f.unlink()
    servers = thai_servers()
    if not servers:
        print("VPN: no Thai servers in VPN Gate's list right now")
        return
    pins = resolve([TRACE, *FEEDS.values()])
    with tempfile.TemporaryDirectory() as d:
        for ip, score, config in servers[:TRIES]:
            if time.time() > START + DEADLINE - CONNECT_WAIT:
                break
            print(f"VPN: trying Thai server {ip} (score {score})")
            saved = try_server(ip, config, pins, Path(d))
            if saved:
                print(f"VPN: Thai server {ip} ok, saved {', '.join(saved)}")
                if len(saved) == len(FEEDS):
                    return
    print("VPN: no Thai server gave every feed; watch.py reads the rest directly")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the job: watch.py carries on and reports the sites it couldn't read
        print(f"VPN: failed: {type(e).__name__}: {e}")
    sys.exit(0)

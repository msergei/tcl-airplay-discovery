#!/usr/bin/python3
"""Публикует в mDNS AirPlay-сервис телевизора, который сам не анонсирует его через USB-LAN.

Каждые POLL секунд спрашивает у телевизора /info (с квалификаторами txtAirPlay/txtRAOP).
Если телевизор отвечает — объявляет в сети _airplay._tcp и _raop._tcp с его же TXT-записями.
Если перестал отвечать — ищет его заново: телевизор по кабелю анонсирует Chromecast/Android
TV Remote, их адреса проверяются по deviceID из /info.
Если телевизор анонсирует AirPlay сам (например, подключён по Wi-Fi) — не дублируем.

Объявления шлются «сырыми» mDNS-пакетами в сеть, а не через mDNSResponder (`dns-sd -P`):
AirPlay на этом же Mac игнорирует сервисы, зарегистрированные локально.
"""
import os
import plistlib
import select
import signal
import socket
import struct
import sys
import threading
import time
import urllib.request

TV_IP = os.environ.get("TV_IP", "")  # необязательная стартовая подсказка, дальше — дисковери
TV_DEVICE_ID = os.environ.get("TV_DEVICE_ID", "").upper()
TV_PORT = int(os.environ.get("TV_PORT", "7000"))
POLL = int(os.environ.get("POLL", "15"))
FAILS_TO_DROP = 3  # столько неудачных проверок подряд — и снимаем публикацию
HOST = "tcl-airplay-proxy.local"
DISCOVERY_TYPES = ["_googlecast._tcp", "_androidtvremote2._tcp"]
MDNS = ("224.0.0.251", 5353)
TTL = 120
ANNOUNCE_EVERY = 30


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def parse_txt(raw):
    out, i = [], 0
    while i < len(raw):
        n = raw[i]
        out.append(raw[i + 1:i + 1 + n].decode("utf-8", "replace"))
        i += 1 + n
    return out


def fetch_info(ip, any_device=False):
    body = plistlib.dumps({"qualifier": ["txtAirPlay", "txtRAOP"]}, fmt=plistlib.FMT_BINARY)
    req = urllib.request.Request(
        f"http://{ip}:{TV_PORT}/info", data=body, method="GET",
        headers={"Content-Type": "application/x-apple-binary-plist", "User-Agent": "AirPlay/377.40"},
    )
    with urllib.request.urlopen(req, timeout=4) as r:
        d = plistlib.loads(r.read())
    if not any_device and d["deviceID"].upper() != TV_DEVICE_ID:
        raise ValueError(f"{ip} is {d['deviceID']}, not the TV")
    return ip, d["name"], d["deviceID"], parse_txt(d["txtAirPlay"]), parse_txt(d["txtRAOP"])


def candidates():
    """IP всех устройств, анонсирующих Chromecast/Android TV Remote."""
    seen = set()
    for stype in DISCOVERY_TYPES:
        for _, _, ips in browse(stype):
            for ip in set(ips) - seen:
                seen.add(ip)
                yield ip


def discover():
    for ip in candidates():
        try:
            return fetch_info(ip)
        except Exception:
            pass
    return None


def find():
    """Режим --find: показать AirPlay-приёмники, найденные через Chromecast/Android TV."""
    found = False
    for ip in candidates():
        try:
            info = fetch_info(ip, any_device=True)
        except Exception:
            continue
        found = True
        print(f"{info[0]:15}  TV_DEVICE_ID={info[2]}  name={info[1]!r}")
    if not found:
        print("ничего не найдено: телевизор включён и в той же сети?")


def tv_advertises_itself(name):
    """Есть ли _airplay._tcp с именем телевизора, опубликованный не нами."""
    full = f"{name}._airplay._tcp.local".lower()
    return any(inst == full and target != HOST for inst, target, _ in browse("_airplay._tcp"))


# --- mDNS ---

def encode_name(labels):
    return b"".join(bytes([len(l.encode())]) + l.encode() for l in labels) + b"\0"


def read_name(pkt, off):
    """Читает имя (с поддержкой сжатия), возвращает (имя в нижнем регистре, смещение после)."""
    labels, end, jumps = [], None, 0
    while True:
        n = pkt[off]
        if n & 0xC0 == 0xC0:
            if end is None:
                end = off + 2
            off = ((n & 0x3F) << 8) | pkt[off + 1]
            jumps += 1
            if jumps > 20:
                raise ValueError("name loop")
            continue
        off += 1
        if n == 0:
            break
        labels.append(pkt[off:off + n].decode("utf-8", "replace"))
        off += n
    return ".".join(labels).lower(), end if end is not None else off


def query_names(pkt):
    if len(pkt) < 12:
        return []
    flags, qd = struct.unpack(">HH", pkt[2:6])
    if flags & 0x8000:  # это ответ, не запрос
        return []
    names, off = [], 12
    for _ in range(qd):
        name, off = read_name(pkt, off)
        off += 4
        names.append(name)
    return names


def parse_records(pkt):
    """Все записи из mDNS-ответа: {(имя, тип, значение)}. PTR → имя, SRV → цель, A → IP."""
    flags, qd, an, ns, ar = struct.unpack(">HHHHH", pkt[2:12])
    if not flags & 0x8000:
        return set()
    off, recs = 12, set()
    for _ in range(qd):
        off = read_name(pkt, off)[1] + 4
    for _ in range(an + ns + ar):
        name, off = read_name(pkt, off)
        rtype, _, ttl, rdlen = struct.unpack(">HHIH", pkt[off:off + 10])
        off += 10
        if ttl:
            if rtype == 12:
                recs.add((name, 12, read_name(pkt, off)[0]))
            elif rtype == 33:
                recs.add((name, 33, read_name(pkt, off + 6)[0]))
            elif rtype == 1 and rdlen == 4:
                recs.add((name, 1, socket.inet_ntoa(pkt[off:off + 4])))
        off += rdlen
    return recs


def mdns_query(questions, seconds=2.0):
    """Шлёт mDNS-запрос [(имя, тип)] и собирает все ответы за seconds секунд."""
    s = mdns_socket(local_ip_towards(MDNS[0]))
    try:
        s.sendto(struct.pack(">HHHHHH", 0, 0, len(questions), 0, 0, 0) + b"".join(
            encode_name(n.split(".")) + struct.pack(">HH", t, 1) for n, t in questions), MDNS)
        recs, deadline = set(), time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0 or not select.select([s], [], [], left)[0]:
                return recs
            try:
                recs |= parse_records(s.recvfrom(9000)[0])
            except Exception:
                pass
    finally:
        s.close()


def browse(stype):
    """[(инстанс, хост, [IP])] для сервиса вида _googlecast._tcp."""
    ptr = f"{stype}.local".lower()
    recs = mdns_query([(ptr, 12)])
    insts = {v for n, t, v in recs if t == 12 and n == ptr}
    srv = lambda: {n: v for n, t, v in recs if t == 33 and n in insts}
    if insts - srv().keys():
        recs |= mdns_query([(i, 33) for i in insts - srv().keys()])
    hosts = set(srv().values())
    ips = lambda: {h: [v for n, t, v in recs if t == 1 and n == h] for h in hosts}
    if any(not v for v in ips().values()):
        recs |= mdns_query([(h, 1) for h, v in ips().items() if not v])
    return [(i, h, ips()[h]) for i, h in srv().items()]


def mdns_socket(iface_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", MDNS[1]))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                 socket.inet_aton(MDNS[0]) + socket.inet_aton(iface_ip))
    return s


def local_ip_towards(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class Publisher:
    """Сам объявляет сервисы в сети и отвечает на mDNS-запросы о них."""

    def __init__(self):
        self.key = None
        self.sock = None
        self.thread = None
        self.stop_evt = threading.Event()

    def _build(self, info, ttl):
        ip, name, device_id, txt_ap, txt_raop = info
        host = HOST.split(".")
        rrs, self.names = [], {HOST.lower()}

        def rr(labels, rtype, data, flush=True):
            cls = 1 | (0x8000 if flush else 0)
            rrs.append(encode_name(labels) + struct.pack(">HHIH", rtype, cls, ttl, len(data)) + data)

        for inst, stype, txt in ((name, "_airplay", txt_ap),
                                 (device_id.replace(":", "") + "@" + name, "_raop", txt_raop)):
            svc = [stype, "_tcp", "local"]
            full = [inst] + svc
            self.names |= {".".join(svc).lower(), ".".join(full).lower()}
            rr(svc, 12, encode_name(full), flush=False)
            rr(full, 33, struct.pack(">HHH", 0, 0, TV_PORT) + encode_name(host))
            rr(full, 16, b"".join(bytes([len(s.encode())]) + s.encode() for s in txt))
        rr(host, 1, socket.inet_aton(ip))
        return struct.pack(">HHHHHH", 0, 0x8400, 0, len(rrs), 0, 0) + b"".join(rrs)

    def _run(self, packet):
        # по RFC 6762: несколько объявлений подряд с растущим интервалом, дальше периодически
        next_at, burst, last_reply = time.time(), [1, 2, 4], 0.0
        while not self.stop_evt.is_set():
            now = time.time()
            if now >= next_at:
                self.sock.sendto(packet, MDNS)
                next_at = now + (burst.pop(0) if burst else ANNOUNCE_EVERY)
            r, _, _ = select.select([self.sock], [], [], max(0.0, min(1.0, next_at - time.time())))
            if not r:
                continue
            try:
                data, _ = self.sock.recvfrom(9000)
                asked = query_names(data)
            except Exception:
                continue
            if any(n in self.names for n in asked) and time.time() - last_reply > 1:
                self.sock.sendto(packet, MDNS)
                last_reply = time.time()

    def start(self, info):
        self.sock = mdns_socket(local_ip_towards(info[0]))
        self.stop_evt.clear()
        self.thread = threading.Thread(target=self._run, args=(self._build(info, TTL),), daemon=True)
        self.thread.start()
        self.key = info
        log(f"published '{info[1]}' -> {info[0]}:{TV_PORT}")

    def stop(self):
        if not self.key:
            return
        self.stop_evt.set()
        self.thread.join(timeout=3)
        try:
            self.sock.sendto(self._build(self.key, 0), MDNS)  # goodbye: TTL 0
        except OSError:
            pass
        self.sock.close()
        self.key = self.sock = self.thread = None
        log("unpublished")

    def alive(self):
        return self.thread is not None and self.thread.is_alive()


def main():
    if "--find" in sys.argv:
        find()
        return
    if not TV_DEVICE_ID:
        sys.exit("TV_DEVICE_ID не задан; узнать его: tcl-airplay-proxy.py --find")
    pub = Publisher()

    def bye(*_):
        pub.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, bye)
    signal.signal(signal.SIGINT, bye)
    log(f"watching {TV_DEVICE_ID} (initial {TV_IP or '-'}), poll {POLL}s")

    ip, fails = TV_IP, 0
    while True:
        try:
            info = fetch_info(ip)
        except Exception as e:
            info = discover()
            if info:
                log(f"TV found at {info[0]} (was {ip})")
                ip = info[0]
            else:
                fails += 1
                if pub.key and fails >= FAILS_TO_DROP:
                    log(f"TV unreachable ({e.__class__.__name__})")
                    pub.stop()

        if info:
            fails = 0
            if info != pub.key or not pub.alive():
                pub.stop()
                if tv_advertises_itself(info[1]):
                    log("TV advertises AirPlay itself, not publishing")
                else:
                    try:
                        pub.start(info)
                    except OSError as e:
                        log(f"publish failed: {e}")
        time.sleep(POLL)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""qh - QEMU harness for agents: boot kernels (aarch64/arm/riscv64/x86_64 IoT-style guests) with an
in-guest bridge agent, fast network, and fast rootfs rebuilds.  stdlib only.

  qh setup                       fetch Alpine userland + build guest agent (once)
  qh up [-n NAME] [--kernel K]   build initramfs, boot VM, wait for agent
  qh exec [-n NAME] CMD...       run a command in the guest (exit code is propagated)
  qh put/get SRC DST             copy files (files or dirs) host<->guest
  qh reload                      rebuild rootfs from overlay dirs + reboot VM (fast loop)
  qh logs [-f]                   serial console log;  qh console  = raw interactive serial
  qh fwd HOST:GUEST              add a host->guest TCP forward at runtime
  qh down | ls | rm | qmp CMD    lifecycle / monitor
Add --json to most commands for machine-readable output.
"""
import argparse, base64, zlib, gzip, io, json, os, shutil, socket, stat, struct, subprocess, sys, tarfile, time, urllib.request, uuid, re

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE_ROOT = os.path.join(ROOT, "cache")
CACHE = os.path.join(CACHE_ROOT, "aarch64")  # per-arch, set by use_arch()
STATE = os.path.join(os.getcwd(), ".qh")
QEMU_DIRS = [r"C:\Program Files\qemu", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"]
ALPINE_BASE = "https://dl-cdn.alpinelinux.org/alpine/"

# Everything arch-specific lives here; every field can be overridden from qh.json / CLI.
ARCHES = {
    "aarch64": dict(qemu="qemu-system-aarch64", alpine="aarch64", goarch="arm64", machine="virt", cpu="max", kflavor="virt",
                    cmdline="console=ttyAMA0 earlycon", chan_tty="/dev/ttyS0"),
    "arm":     dict(qemu="qemu-system-arm", alpine="armv7", goarch="arm", goarm="7", machine="virt", cpu="max", kflavor="lts",
                    cmdline="console=ttyAMA0", chan_tty=None),
    "riscv64": dict(qemu="qemu-system-riscv64", alpine="riscv64", goarch="riscv64", machine="virt", cpu="rv64", kflavor="lts",
                    cmdline="console=ttyS0 earlycon=sbi", chan_tty=None),
    "x86_64":  dict(qemu="qemu-system-x86_64", alpine="x86_64", goarch="amd64", machine="q35", cpu="max", kflavor="virt",
                    cmdline="console=ttyS0", chan_tty=None),
}
ALIASES = {"arm64": "aarch64", "armv8": "aarch64", "arm32": "arm", "armv7": "arm", "armhf": "arm", "riscv": "riscv64",
           "amd64": "x86_64", "x64": "x86_64", "x86": "x86_64"}
ARCH = dict(ARCHES["aarch64"], name="aarch64")
DEFAULT_CFG = {"arch": None, "kernel": None, "mem": "512M", "smp": 2, "machine": None, "cpu": None, "fs": [], "pkgs": [],
               "fwd": ["tcp::2222-:22"], "append": "", "net": "static", "disk": None, "dtb": None, "initrd": None, "bios": None,
               "chan": "auto", "nic": "auto", "bus": "auto", "chan_tty": None}


def norm_arch(x):
    x = ALIASES.get(x.lower(), x.lower())
    if x not in ARCHES: die(f"unknown arch '{x}' (have: {', '.join(ARCHES)})")
    return x


def use_arch(name):
    global ARCH, CACHE
    name = norm_arch(name)
    ARCH = dict(ARCHES[name], name=name); CACHE = os.path.join(CACHE_ROOT, name); os.makedirs(CACHE, exist_ok=True)


def read_kernel(path):
    """Kernel file bytes, transparently gunzipped / EFI-zboot unwrapped."""
    data = open(path, "rb").read()
    if data[:2] == b"\x1f\x8b": data = gzip.decompress(data)
    return data


def detect_arch(path):
    """Guess the architecture from the kernel image header (Image / zImage / bzImage / ELF)."""
    d = read_kernel(path)[:0x1000]
    if d[56:60] == b"ARM\x64": return "aarch64"
    if len(d) > 0x28 and struct.unpack_from("<I", d, 0x24)[0] == 0x016F2818: return "arm"
    if d[0x38:0x3c] == b"RSC\x05": return "riscv64"
    if d[0x202:0x206] == b"HdrS": return "x86_64"
    if d[:4] == b"\x7fELF":
        return {183: "aarch64", 40: "arm", 243: "riscv64", 62: "x86_64"}.get(struct.unpack_from("<H", d, 18)[0])
    return None


def die(msg, code=1):
    print(f"qh: {msg}", file=sys.stderr); sys.exit(code)


def load_cfg(args, base_cfg=None):
    """qh.json (cwd, then harness dir) < CLI flags.  With base_cfg (reload) the saved VM config is the base."""
    cfg = dict(DEFAULT_CFG)
    if base_cfg is not None:
        cfg.update(base_cfg); base = os.getcwd()
    else:
        for p in (os.path.join(os.getcwd(), "qh.json"), os.path.join(ROOT, "qh.json"), os.path.join(ROOT, "qh.example.json")):
            if os.path.exists(p):
                with open(p) as f: cfg.update(json.load(f))
                cfg["_base"] = os.path.dirname(p); break
        base = cfg.get("_base", os.getcwd())
    for k in ("arch", "kernel", "mem", "smp", "machine", "cpu", "append", "net", "disk", "dtb", "initrd", "bios", "chan", "nic", "bus", "chan_tty"):
        v = getattr(args, k, None)
        if v is not None: cfg[k] = v
    if getattr(args, "pkg", None): cfg["pkgs"] = list(cfg["pkgs"]) + args.pkg
    if getattr(args, "fs", None): cfg["fs"] = list(cfg["fs"]) + [os.path.abspath(d) for d in args.fs]
    if getattr(args, "fwd", None): cfg["fwd"] = args.fwd
    for k in ("kernel", "disk", "dtb", "initrd", "bios"):
        if cfg[k] and not str(cfg[k]).startswith("alpine:") and not os.path.isabs(cfg[k]): cfg[k] = os.path.join(base, cfg[k])
    cfg["fs"] = [d if os.path.isabs(d) else os.path.join(base, d) for d in cfg["fs"]]
    # kernel spec "alpine:ARCH[:flavor]" -> fetched Alpine kernel + its virtio modules
    k = cfg["kernel"]
    if k and k.startswith("alpine:"):
        parts = k.split(":")
        arch = norm_arch(parts[1] if len(parts) > 1 and parts[1] else (cfg["arch"] or "aarch64"))
        cfg["arch"] = arch; use_arch(arch)
        kp, modfs = kernel_fetch(arch, parts[2] if len(parts) > 2 else None)
        cfg["kernel"] = kp
        if modfs not in cfg["fs"]: cfg["fs"].append(modfs)
        cfg["_mods"] = True
    if cfg["kernel"] and not cfg["arch"]:
        if not os.path.exists(cfg["kernel"]): die(f"kernel not found: {cfg['kernel']}")
        cfg["arch"] = detect_arch(cfg["kernel"]) or "aarch64"
    cfg["arch"] = norm_arch(cfg["arch"] or "aarch64"); use_arch(cfg["arch"])
    for k2 in ("machine", "cpu", "chan_tty"):
        if cfg[k2] is None: cfg[k2] = ARCH.get(k2)
    cfg["_mods"] = cfg.get("_mods") or any(os.path.isdir(os.path.join(d, "lib", "modules")) for d in cfg["fs"])
    return cfg


def qemu_bin(name=None):
    name = name or ARCH["qemu"]
    p = shutil.which(name)
    if p: return p
    for d in QEMU_DIRS:
        for ext in ("", ".exe"):
            c = os.path.join(d, name + ext)
            if os.path.exists(c): return c
    die(f"{name} not found in PATH")


# ---------------------------------------------------------------- setup / rootfs
def ensure_setup(arch, force=False):
    use_arch(arch)
    tarp = os.path.join(CACHE, "alpine-minirootfs.tar.gz"); rel = ALPINE_BASE + f"latest-stable/releases/{ARCH['alpine']}/"
    if not os.path.exists(tarp) or force:
        idx = urllib.request.urlopen(rel).read().decode()
        names = sorted(set(re.findall(r'href="(alpine-minirootfs-\d+\.\d+\.\d+-' + ARCH["alpine"] + r'\.tar\.gz)"', idx)))
        if not names: die(f"no alpine minirootfs found for {ARCH['alpine']}")
        print("downloading", names[-1], file=sys.stderr); urllib.request.urlretrieve(rel + names[-1], tarp)
    agent = os.path.join(CACHE, "qh-agent")
    srcs = [os.path.join(ROOT, "agent", "main.go")]
    if force or not os.path.exists(agent) or os.path.getmtime(agent) < max(os.path.getmtime(x) for x in srcs):
        env = dict(os.environ, GOOS="linux", GOARCH=ARCH["goarch"], CGO_ENABLED="0")
        if ARCH.get("goarm"): env["GOARM"] = ARCH["goarm"]
        print(f"building guest agent ({ARCH['goarch']})", file=sys.stderr)
        r = subprocess.run(["go", "build", "-ldflags=-s -w", "-o", agent, "."], cwd=os.path.join(ROOT, "agent"), env=env)
        if r.returncode: die("go build failed")


def cmd_setup(a):
    for ar in (list(ARCHES) if a.arch == "all" else [norm_arch(a.arch)]):
        ensure_setup(ar, a.force); print(f"setup ok: {ar}")


# ---------------------------------------------------------------- apk packages (resolved/unpacked on the host, no apk needed)
def _alpine_branch():
    with tarfile.open(os.path.join(CACHE, "alpine-minirootfs.tar.gz")) as t:
        ver = t.extractfile("./etc/alpine-release").read().decode().strip()
    return "v" + ".".join(ver.split(".")[:2])


def _parse_index(txt):
    out = []
    for blk in txt.split(chr(10) * 2):
        d = {}
        for ln in blk.splitlines():
            if len(ln) > 2 and ln[1] == ":": d[ln[0]] = ln[2:]
        if "P" in d: out.append(d)
    return out


def _tok(x): return re.split(r"[=<>~]", x, 1)[0]


def apk_index():
    br = _alpine_branch(); os.makedirs(os.path.join(CACHE, "apk"), exist_ok=True)
    pkgs = []
    for repo in ("main", "community"):
        f = os.path.join(CACHE, "apk", f"APKINDEX-{br}-{repo}.tar.gz")
        if not os.path.exists(f) or time.time() - os.path.getmtime(f) > 7 * 86400:
            urllib.request.urlretrieve(f"{ALPINE_BASE}{br}/{repo}/{ARCH['alpine']}/APKINDEX.tar.gz", f)
        with tarfile.open(f) as t:
            for p in _parse_index(t.extractfile("APKINDEX").read().decode()): p["repo"] = repo; pkgs.append(p)
    prov = {}
    for p in pkgs:
        prov.setdefault(p["P"], p)
        for x in p.get("p", "").split(): prov.setdefault(_tok(x), p)
    return br, prov


def apk_download(p, br):
    f = os.path.join(CACHE, "apk", f"{p['P']}-{p['V']}.apk")
    if not os.path.exists(f):
        print(f"apk: fetching {p['P']}-{p['V']}", file=sys.stderr)
        urllib.request.urlretrieve(f"{ALPINE_BASE}{br}/{p['repo']}/{ARCH['alpine']}/{p['P']}-{p['V']}.apk", f)
    return f


def apk_segments(f):
    raw = open(f, "rb").read(); segs = []
    while raw:  # apk = concatenated gzip streams: [signature] control data
        d = zlib.decompressobj(31); segs.append(d.decompress(raw)); raw = d.unused_data
    return segs


def apk_resolve(names):
    """-> list of (pkgname, version, repo) needed on top of the base minirootfs."""
    br, prov = apk_index()
    have = set()
    with tarfile.open(os.path.join(CACHE, "alpine-minirootfs.tar.gz")) as t:
        for blk in _parse_index(t.extractfile("./lib/apk/db/installed").read().decode()):
            have.add(blk["P"]); have.update(_tok(x) for x in blk.get("p", "").split())
    order, seen = [], set()
    def visit(n):
        if n in have or n in seen or n.startswith("!"): return
        seen.add(n)
        p = prov.get(n) or die(f"apk: no package provides '{n}'")
        if p["P"] in have: return
        for d in p.get("D", "").split(): visit(_tok(d))
        if p["P"] not in [o[0] for o in order]: order.append((p["P"], p["V"], p["repo"]))
    for n in names: visit(n)
    return order


def apk_entries(names):
    """Unpack the data segment of each .apk -> {path: (mode, data)}; cached per package list."""
    import pickle, hashlib
    if not names: return {}
    key = hashlib.sha1((chr(10).join(sorted(names)) + _alpine_branch()).encode()).hexdigest()[:12]
    cf = os.path.join(CACHE, "apk", f"ents-{key}.pickle")
    if os.path.exists(cf): return pickle.load(open(cf, "rb"))
    br, prov = apk_index(); ents = {}
    for name, ver, repo in apk_resolve(names):
        f = apk_download(dict(P=name, V=ver, repo=repo), br)
        with tarfile.open(fileobj=io.BytesIO(apk_segments(f)[-1])) as t:
            for m in t:
                p = m.name.lstrip("./").rstrip("/")
                if not p or p.startswith("."): continue
                if m.isdir(): ents[p] = (stat.S_IFDIR | (m.mode & 0o7777), b"")
                elif m.issym(): ents[p] = (stat.S_IFLNK | 0o777, m.linkname.encode())
                elif m.isreg(): ents[p] = (stat.S_IFREG | (m.mode & 0o7777), t.extractfile(m).read())
                elif m.islnk() and m.linkname.lstrip("./") in ents: ents[p] = ents[m.linkname.lstrip("./")]
    pickle.dump(ents, open(cf, "wb"))
    return ents


def _newc(out, ino, name, mode, data=b"", nlink=1, rdev=(0, 0)):
    nb = name.encode() + b"\0"
    h = "070701" + "".join("%08x" % v for v in (ino, mode, 0, 0, nlink, 0, len(data), 0, 0, rdev[0], rdev[1], len(nb), 0))
    out.write(h.encode() + nb); out.write(b"\0" * ((4 - (110 + len(nb)) % 4) % 4))
    out.write(data); out.write(b"\0" * ((4 - len(data) % 4) % 4))


def build_initramfs(dest, fs_dirs, extra_files=None, pkgs=()):
    """Alpine base tar + project overlay + user fs dirs (later wins) -> cpio.gz."""
    tarp = os.path.join(CACHE, "alpine-minirootfs.tar.gz"); agent = os.path.join(CACHE, "qh-agent")
    if not (os.path.exists(tarp) and os.path.exists(agent)): die("run `qh setup` first")
    ents = {}  # path -> (mode, data)
    with tarfile.open(tarp) as t:
        for m in t:
            p = m.name.lstrip("./").rstrip("/")
            if not p: continue
            if m.isdir(): ents[p] = (stat.S_IFDIR | (m.mode & 0o7777), b"")
            elif m.issym(): ents[p] = (stat.S_IFLNK | 0o777, m.linkname.encode())
            elif m.isreg(): ents[p] = (stat.S_IFREG | (m.mode & 0o7777), t.extractfile(m).read())
        for m in t.getmembers():  # hardlinks -> copies
            if m.islnk():
                src = ents.get(m.linkname.lstrip("./"))
                if src: ents[m.name.lstrip("./")] = src
    for p, e in apk_entries(list(pkgs)).items(): ents[p] = e
    def addtree(d):
        for dp, dns, fns in os.walk(d):
            rel = os.path.relpath(dp, d).replace("\\", "/")
            if rel != ".": ents[rel] = (stat.S_IFDIR | 0o755, b"")
            for fn in fns:
                full = os.path.join(dp, fn); r = (rel + "/" + fn) if rel != "." else fn
                data = open(full, "rb").read()
                if data.startswith(b"#!"): data = data.replace(bytes([13, 10]), bytes([10]))  # CRLF checkouts must not break guest scripts
                exe = r in ("init",) or r.split("/")[0] in ("bin", "sbin") or "/bin/" in "/" + r or r.endswith(".sh") or data.startswith(b"#!") or data.startswith(b"\x7fELF")
                ents[r] = (stat.S_IFREG | (0o755 if exe else 0o644), data)
    addtree(os.path.join(ROOT, "overlay"))
    ents["usr/local/bin/qh-agent"] = (stat.S_IFREG | 0o755, open(agent, "rb").read())
    for d in fs_dirs:
        if not os.path.isdir(d): die(f"fs dir not found: {d}")
        addtree(d)
    for p, (mode, data) in (extra_files or {}).items(): ents[p] = (mode, data)
    ents["dev/console"] = (stat.S_IFCHR | 0o600, b"", (5, 1)); ents["dev/null"] = (stat.S_IFCHR | 0o666, b"", (1, 3))
    for d in ("proc", "sys", "dev", "tmp", "run", "mnt"):
        ents.setdefault(d, (stat.S_IFDIR | 0o755, b""))
    buf = io.BytesIO(); ino = 1
    for p in sorted(ents, key=lambda s: (s.count("/"), s)):
        e = ents[p]
        _newc(buf, ino, p, e[0], e[1], rdev=e[2] if len(e) > 2 else (0, 0)); ino += 1
    _newc(buf, 0, "TRAILER!!!", 0)
    with open(dest, "wb") as f:
        f.write(gzip.compress(buf.getvalue(), 1))
    return len(ents)


# ---------------------------------------------------------------- kernel probing
def kernel_config(path):
    """{CONFIG_X: 'y'|'m'} from `<kernel>.config` (written by `kernel fetch`) or the IKCFG blob inside the image; None if unknown."""
    txt = None
    if os.path.exists(path + ".config"): txt = open(path + ".config", errors="replace").read()
    else:
        data = read_kernel(path); i = data.find(b"IKCFG_ST")
        if i >= 0:
            try: txt = zlib.decompressobj(31).decompress(data[i + 8:i + 8 + 4_000_000]).decode()
            except zlib.error: pass
    return dict(re.findall(r"^(CONFIG_\w+)=([ym])$", txt, re.M)) if txt else None


def pick_transport(cfg, kcfg):
    """-> (chan, nic, bus).  chan: virtio | pci-serial | serial (2nd UART, needs chan_tty).  nic: native | tap | none.
    bus: pci | mmio (which virtio transport the kernel has).  Each can be forced in qh.json / CLI."""
    k = kcfg or {}; mods = cfg.get("_mods")
    def have(sym): return k.get(sym) == "y" or (mods and k.get(sym) == "m")
    unknown = kcfg is None
    bus = cfg["bus"]
    if bus == "auto":
        pci_ok = have("CONFIG_VIRTIO_PCI") and (cfg["arch"] == "x86_64" or have("CONFIG_PCI_HOST_GENERIC") or have("CONFIG_PCI_HOST_COMMON"))
        bus = "pci" if (pci_ok or unknown) else "mmio" if have("CONFIG_VIRTIO_MMIO") else None
    chan, nic = cfg["chan"], cfg["nic"]
    if chan == "auto":
        if bus and (have("CONFIG_VIRTIO_CONSOLE") or (unknown and mods)): chan = "virtio"
        elif cfg["chan_tty"] and cfg["chan_tty"] == ARCH.get("chan_tty") and (k.get("CONFIG_SERIAL_8250_PCI") == "y" or unknown): chan = "pci-serial"
        elif cfg["chan_tty"]: chan = "serial"
        else: die("no usable agent channel: kernel lacks virtio-console (and no modules shipped) and no chan_tty is known for this arch.\n"
                  "  options: --kernel alpine:<arch> (kernel + modules), ship virtio modules in an fs dir (lib/modules/...),\n"
                  "  or set chan=serial + chan_tty=/dev/ttyXX for a board with a spare UART, or --no-wait + --initrd for console-only use")
    if nic == "auto":
        if bus and have("CONFIG_VIRTIO_NET"): nic = "native"
        elif have("CONFIG_TUN") or (unknown and not mods): nic = "tap"
        else: nic = "none"
    return chan, nic, bus or "pci"


def kernel_fetch(arch, flavor=None, force=False):
    """Download Alpine's kernel package for ARCH -> (kernel path, modfs dir with the virtio/tun/9p module closure)."""
    arch = norm_arch(arch); ensure_setup(arch); flavor = flavor or ARCH["kflavor"]
    kd = os.path.join(CACHE, "kernels", flavor); kp = os.path.join(kd, "kernel"); mfs = os.path.join(kd, "modfs")
    if os.path.exists(kp) and os.path.isdir(mfs) and not force: return kp, mfs
    br, prov = apk_index(); p = prov.get("linux-" + flavor) or die(f"no linux-{flavor} for {ARCH['alpine']}")
    f = apk_download(p, br); shutil.rmtree(kd, ignore_errors=True); os.makedirs(mfs)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(apk_segments(f)[-1])) as t:
        for m in t:
            if m.isreg(): files[m.name.lstrip("./")] = t.extractfile(m).read()
    vm = [n for n in files if n.startswith("boot/vmlinuz")] or die("no vmlinuz in kernel package")
    img = files[vm[0]]
    if img[:2] == b"\x1f\x8b": img = gzip.decompress(img)
    if img[4:8] == b"zimg":  # EFI zboot wrapper: payload offset/size/compression type in header
        off, size = struct.unpack_from("<II", img, 8); typ = img[24:32].rstrip(b"\0")
        payload = img[off:off + size]
        if typ == b"gzip": img = gzip.decompress(payload)
        else: die(f"EFI zboot kernel uses {typ.decode()} compression; not supported by this harness")
    open(kp, "wb").write(img)
    cf = [n for n in files if n.startswith("boot/config-")]
    if cf: open(kp + ".config", "wb").write(files[cf[0]])
    ver = [n.split("/")[2] for n in files if re.match(r"lib/modules/[^/]+/modules\.dep$", n)]
    if ver:
        ver = ver[0]; base = f"lib/modules/{ver}/"
        dep = {}
        for ln in files[base + "modules.dep"].decode().splitlines():
            if ":" in ln:
                k, v = ln.split(":", 1); dep[k.strip()] = v.split()
        seeds = ("virtio_pci", "virtio_mmio", "virtio_console", "virtio_net", "virtio_blk", "virtio_rng", "virtio_balloon", "virtio-rng",
                 "9pnet_virtio", "9p", "tun", "af_packet", "veth", "dummy", "bridge", "overlay", "loop", "ext4", "squashfs", "vfat", "nls_cp437")
        want, stack = set(), [k for k in dep if re.sub(r"\.ko(\.\w+)?$", "", os.path.basename(k)).replace("-", "_") in
                              [x.replace("-", "_") for x in seeds]]
        while stack:
            k = stack.pop()
            if k in want: continue
            want.add(k); stack += [d for d in dep.get(k, [])]
        for n, data in files.items():
            if n.startswith(base) and (n[len(base):] in want or n.count("/") == 3 and n.split("/")[-1].startswith("modules.")):
                nm = n.split("/")[-1]
                if nm.endswith(".bin"): continue
                if nm.startswith("modules."):  # text maps: point at the decompressed .ko names
                    data = re.sub(rb"\.ko\.(gz|zst|xz)", b".ko", data)
                elif nm.endswith(".ko.gz"): data = gzip.decompress(data); n = n[:-3]
                elif nm.endswith((".ko.zst", ".ko.xz")): die("module compression other than gzip is not supported")
                dst = os.path.join(mfs, *n.split("/")); os.makedirs(os.path.dirname(dst), exist_ok=True); open(dst, "wb").write(data)
    print(f"kernel: {kp} ({len(img)//1024} KiB), modules: {len(want) if ver else 0}", file=sys.stderr)
    return kp, mfs


# ---------------------------------------------------------------- daemon (one per VM)
def run_daemon(name):
    """Owns the agent channel: multiplexes RPC from many CLI clients and tunnels ethernet frames
    between the guest tap and QEMU's slirp (netdev stream)."""
    import asyncio
    sys.stdout = sys.stderr = open(os.path.join(sdir(name), "daemon.log"), "w", buffering=1)
    st = json.load(open(os.path.join(sdir(name), "state.json")))
    pending = {}

    async def connect(port):
        while True:
            try: return await asyncio.open_connection("127.0.0.1", port, limit=1 << 25)
            except OSError: await asyncio.sleep(0.2)

    async def main():
        chr_, chw = await connect(st["chan_port"])
        net = {"w": None}

        async def chan_write(b):
            chw.write(b); await chw.drain()

        async def chan_loop():
            while True:
                line = await chr_.readline()
                if not line: return
                if line[:1] == b"F":
                    w = net["w"]
                    if w is not None:
                        try:
                            fr = base64.b64decode(line[1:].strip()); w.write(struct.pack(">I", len(fr)) + fr)
                        except Exception: pass
                elif line[:1] == b"{":
                    try: rid = json.loads(line).get("id")
                    except ValueError: continue
                    w = pending.pop(rid, None)
                    if w is not None:
                        try: w.write(line); await w.drain()
                        except OSError: pass

        async def net_loop():
            if not st.get("net_port"): return
            while True:
                r, w = await connect(st["net_port"]); net["w"] = w
                try:
                    while True:
                        n = struct.unpack(">I", await r.readexactly(4))[0]
                        await chan_write(b"F" + base64.b64encode(await r.readexactly(n)) + b"\n")
                except (asyncio.IncompleteReadError, OSError):
                    net["w"] = None; await asyncio.sleep(0.5)

        async def client(r, w):
            mine = set()
            try:
                while True:
                    line = await r.readline()
                    if not line: break
                    try: rid = json.loads(line)["id"]
                    except (ValueError, KeyError): continue
                    pending[rid] = w; mine.add(rid)
                    await chan_write(line if line.endswith(b"\n") else line + b"\n")
            except OSError: pass
            finally:
                for i in mine: pending.pop(i, None)
                w.close()

        async def watchdog():
            while pid_alive(st["pid"]): await asyncio.sleep(2)
            os._exit(0)

        srv = await asyncio.start_server(client, "127.0.0.1", st["agent_port"], limit=1 << 25)
        await asyncio.gather(chan_loop(), net_loop(), watchdog(), srv.serve_forever())

    asyncio.run(main())


# ---------------------------------------------------------------- instance state
def sdir(name): return os.path.join(STATE, name)


def load(name, must_run=False):
    p = os.path.join(sdir(name), "state.json")
    if not os.path.exists(p): die(f"no instance '{name}' (see `qh ls`)")
    s = json.load(open(p))
    s["alive"] = pid_alive(s.get("pid"))
    if must_run and not s["alive"]: die(f"instance '{name}' is not running")
    return s


def pid_alive(pid):
    if not pid: return False
    if os.name == "nt":  # no subprocess: spawning tasklist from a detached process pops up console windows
        import ctypes
        k = ctypes.windll.kernel32; k.OpenProcess.restype = ctypes.c_void_p
        h = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h: return False
        code = ctypes.c_ulong(); k.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)); k.CloseHandle(ctypes.c_void_p(h))
        return code.value == 259  # STILL_ACTIVE
    try: os.kill(pid, 0); return True
    except OSError: return False


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def default_name():
    if os.path.isdir(STATE):
        names = [n for n in os.listdir(STATE) if os.path.exists(os.path.join(STATE, n, "state.json"))]
        if len(names) == 1: return names[0]
    return "default"


# ---------------------------------------------------------------- agent client
class Agent:
    def __init__(self, port, timeout=10):
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.buf = b""

    def call(self, wait=30, **req):
        req["id"] = uuid.uuid4().hex[:12]
        self.s.settimeout(wait)
        self.s.sendall(json.dumps(req).encode() + b"\n")
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                try: r = json.loads(line)
                except ValueError: continue
                if r.get("id") == req["id"]: return r
            d = self.s.recv(1 << 20)
            if not d: raise ConnectionError("agent connection closed")
            self.buf += d

    def close(self): self.s.close()


def agent_for(s, timeout=10):
    try: return Agent(s["agent_port"], timeout)
    except OSError as e: die(f"cannot reach VM agent: {e}")


def b64d(x): return base64.b64decode(x) if x else b""


# ---------------------------------------------------------------- commands
def cmd_build(a):
    cfg = load_cfg(a); out = a.out or os.path.join(STATE, a.name, "rootfs.cpio.gz")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    t = time.time(); n = build_initramfs(out, cfg["fs"], pkgs=cfg.get("pkgs", []))
    print(f"built {out} ({n} entries, {os.path.getsize(out)//1024} KiB, {time.time()-t:.1f}s)")


CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW, CREATE_BREAKAWAY_FROM_JOB = 0x00000200, 0x08000000, 0x01000000


class _Proc:
    """Minimal Popen stand-in for processes started through WMI (no handle, only a pid)."""
    def __init__(self, pid): self.pid = pid
    def poll(self): return None if pid_alive(self.pid) else 0


def _wmi_spawn(argv, cwd):
    """Start a process via WMI Win32_Process.Create: its parent is the WMI service, so it is outside the caller's Job Object
    (terminals/agent runners often wrap commands in a KILL_ON_JOB_CLOSE job that does not allow breakaway)."""
    import base64 as b64
    cl = subprocess.list2cmdline(argv)
    ps = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0}; "
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=%s; CurrentDirectory=%s; ProcessStartupInformation=$si}; "
          "if ($r.ReturnValue -ne 0) { exit 1 }; $r.ProcessId") % ("'" + cl.replace("'", "''") + "'", "'" + cwd.replace("'", "''") + "'")
    enc = b64.b64encode(ps.encode("utf-16-le")).decode()
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], capture_output=True, text=True,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode or not r.stdout.strip().isdigit(): die("could not start process via WMI: " + (r.stderr or r.stdout)[-300:])
    return _Proc(int(r.stdout.strip()))


def spawn_detached(argv, logpath=None, cwd=None):
    """Start a long-lived background process that survives this command (and its job/console) exiting."""
    cwd = cwd or os.getcwd()
    if os.name != "nt":
        lf = open(logpath, "wb") if logpath else subprocess.DEVNULL
        return subprocess.Popen(argv, cwd=cwd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, close_fds=True, start_new_session=True)
    base = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    lf = open(logpath, "wb") if logpath else subprocess.DEVNULL
    for extra in (CREATE_BREAKAWAY_FROM_JOB, None):
        if extra is None: break
        try:
            return subprocess.Popen(argv, cwd=cwd, stdout=lf, stderr=lf, stdin=subprocess.DEVNULL, creationflags=base | extra, close_fds=True)
        except OSError:  # the enclosing job forbids breakaway
            pass
    return _wmi_spawn(argv, cwd)  # output of the child is not captured on this path


def build_qemu_cmd(a, cfg, name, d, ports, chan, nic, bus, hostfwd, initrd):
    kernel = cfg["kernel"]; log = os.path.join(d, "console.log")
    dev = "pci" if bus == "pci" else "device"
    tty = cfg["chan_tty"] or "/dev/ttyS0"
    append = (f"{ARCH['cmdline']} rdinit=/init qh.net={cfg['net']} qh.hostname={name} qh.nic={nic} "
              f"qh.chan={'virtio' if chan == 'virtio' else tty} loglevel=4 ")
    if chan == "pci-serial": append += "8250.nr_uarts=4 "
    append += cfg["append"]
    q = [qemu_bin(), "-M", cfg["machine"], "-cpu", cfg["cpu"], "-m", cfg["mem"], "-smp", str(cfg["smp"]),
         "-kernel", kernel, "-append", append, "-display", "none", "-monitor", "none", "-name", name,
         "-chardev", f"socket,id=ser0,host=127.0.0.1,port={ports['serial_port']},server=on,wait=off,logfile={log}",
         "-serial", "chardev:ser0",
         "-qmp", f"tcp:127.0.0.1:{ports['qmp_port']},server=on,wait=off"]
    if initrd: q += ["-initrd", initrd]
    if cfg["bios"]: q += ["-bios", cfg["bios"]]
    if chan != "none":
        q += ["-chardev", f"socket,id=qh0,host=127.0.0.1,port={ports['chan_port']},server=on,wait=off"]
    if chan == "virtio":
        q += ["-device", f"virtio-serial-{dev}", "-device", "virtserialport,chardev=qh0,name=qh.agent"]
    elif chan == "pci-serial":  # 16550 behind PCI; raise baudbase so QEMU does not throttle the link to 115200 baud
        q += ["-device", f"pci-serial,chardev=qh0,baudbase={115200 * 256}"]
    elif chan == "serial":  # a spare board UART (second -serial), e.g. ttyAMA1
        q += ["-serial", "chardev:qh0"]
    user = "user,id=u0" + "".join(f",hostfwd={h}" for h in hostfwd)
    if nic == "native":
        q += ["-netdev", user, "-device", f"virtio-net-{dev},netdev=u0"]
    elif nic == "tap":  # slirp <-> hub <-> stream socket <-> daemon <-> channel <-> guest tap
        q += ["-netdev", user, "-netdev", f"stream,id=s0,addr.type=inet,addr.host=127.0.0.1,addr.port={ports['net_port']},server=on",
              "-netdev", "hubport,id=h0,hubid=0,netdev=u0", "-netdev", "hubport,id=h1,hubid=0,netdev=s0"]
    if chan == "virtio" or nic == "native": q += ["-device", f"virtio-rng-{dev}"]
    if cfg["dtb"]: q += ["-dtb", cfg["dtb"]]
    if cfg["disk"]:
        q += ["-drive", f"if=none,file={cfg['disk']},id=d0,format={'qcow2' if cfg['disk'].endswith('qcow2') else 'raw'}",
              "-device", f"virtio-blk-{dev},drive=d0"]
    return q + (a.extra or [])


def launch(a, cfg, name, prev=None):
    d = sdir(name); os.makedirs(d, exist_ok=True)
    kernel = cfg["kernel"] or die("no kernel: pass --kernel (file or alpine:<arch>) or set it in qh.json")
    if not os.path.exists(kernel): die(f"kernel not found: {kernel}")
    ensure_setup(cfg["arch"])
    kcfg = kernel_config(kernel); chan, nic, bus = pick_transport(cfg, kcfg)
    ports = {k: (prev or {}).get(k) or free_port() for k in ("agent_port", "chan_port", "qmp_port", "serial_port")}
    ports["net_port"] = ((prev or {}).get("net_port") or free_port()) if nic == "tap" else 0
    hostfwd = []
    for f in cfg["fwd"]:  # "2222:22" or "udp:1040:1040" shorthand => tcp::2222-:22 ; host port 0 => auto ; raw "tcp::2222-:22" passes through
        if "::" not in f:
            proto = "tcp"; parts = f.split(":")
            if parts[0] in ("tcp", "udp"): proto = parts.pop(0)
            h, g = parts; f = f"{proto}::{h if h != '0' else free_port()}-:{g}"
        hostfwd.append(f)
    initrd = cfg["initrd"]; tb = 0
    if not initrd:
        initrd = os.path.join(d, "rootfs.cpio.gz")
        t = time.time(); build_initramfs(initrd, cfg["fs"], pkgs=cfg.get("pkgs", [])); tb = time.time() - t
    q = build_qemu_cmd(a, cfg, name, d, ports, chan, nic, bus, hostfwd, initrd)
    if getattr(a, "dry_run", False):
        print(" ".join(f'"{x}"' if " " in x else x for x in q)); print(f"# arch={cfg['arch']} chan={chan} nic={nic} bus={bus}"); sys.exit(0)
    log = os.path.join(d, "console.log")
    if os.path.exists(log): os.remove(log)
    p = spawn_detached(q, os.path.join(d, "qemu.log"))
    if prev and pid_alive(prev.get("daemon_pid")): kill_pid(prev["daemon_pid"])
    st = dict(ports, name=name, pid=p.pid, arch=cfg["arch"], chan=chan, nic=nic, bus=bus, kernel=kernel, cmd=q, fwd=hostfwd,
              started=time.time(), fs=cfg["fs"], cfg={k: v for k, v in cfg.items() if not k.startswith("_")}, build_s=round(tb, 2))
    sp_ = os.path.join(d, "state.json")
    json.dump(st, open(sp_, "w"), indent=1)  # daemon reads this
    dp = spawn_detached([sys.executable, os.path.abspath(__file__), "_daemon", name], os.path.join(d, "daemon.log"))
    st["daemon_pid"] = dp.pid
    json.dump(st, open(sp_, "w"), indent=1)
    return st, p


def kill_pid(pid):
    if os.name == "nt": subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, creationflags=0x08000000)
    else:
        try: os.kill(pid, 9)
        except OSError: pass


def wait_agent(st, p, timeout):
    t0 = time.time(); log = os.path.join(sdir(st["name"]), "console.log")
    while time.time() - t0 < timeout:
        if p is not None and p.poll() is not None or not pid_alive(st["pid"]):
            tail = open(os.path.join(sdir(st["name"]), "qemu.log"), errors="replace").read()[-1500:]
            die("qemu exited early:\n" + tail)
        try:
            ag = Agent(st["agent_port"], 2)
            r = ag.call(wait=1.5, op="ping"); ag.close()
            if r.get("ok"): return time.time() - t0, r
        except (OSError, ConnectionError): pass
        time.sleep(0.3)
    tail = open(log, errors="replace").read()[-2000:] if os.path.exists(log) else ""
    die(f"agent did not respond within {timeout}s. console tail:\n{tail}")


def cmd_up(a):
    cfg = load_cfg(a); name = a.name
    if os.path.exists(os.path.join(sdir(name), "state.json")):
        old = load(name)
        if old["alive"]:
            if not a.restart: die(f"'{name}' already running (use `qh reload` or `qh down`)")
            kill(old)
    st, p = launch(a, cfg, name)
    if a.no_wait: return emit(a, {"name": name, "pid": st["pid"], "agent_port": st["agent_port"]})
    dt, _ = wait_agent(st, p, a.timeout)
    emit(a, {"name": name, "pid": st["pid"], "boot_s": round(dt, 1), "rootfs_build_s": st["build_s"],
             "forwards": st["fwd"], "agent_port": st["agent_port"], "serial_port": st["serial_port"],
             "console_log": os.path.join(sdir(name), "console.log")})


def kill(s):
    try:
        ag = Agent(s["qmp_port"], 2); ag.s.recv(4096)
        ag.s.sendall(b'{"execute":"qmp_capabilities"}\n{"execute":"quit"}\n'); time.sleep(0.3); ag.close()
    except OSError: pass
    t = time.time()
    while pid_alive(s["pid"]) and time.time() - t < 3: time.sleep(0.1)
    if pid_alive(s["pid"]):
        kill_pid(s["pid"])


def cmd_down(a):
    s = load(a.name)
    if s["alive"]: kill(s)
    emit(a, {"name": a.name, "stopped": True})


def cmd_rm(a):
    s = load(a.name)
    if s["alive"]: kill(s)
    shutil.rmtree(sdir(a.name)); emit(a, {"removed": a.name})


def cmd_reload(a):
    """Rebuild rootfs and restart the VM (same ports).  Any VM option (--kernel/--dtb/--machine/--append/--fs ...) may be changed."""
    s = load(a.name); cfg = load_cfg(a, base_cfg=s["cfg"])
    cfg["fs"] = list(dict.fromkeys(cfg["fs"]))
    if s["alive"]: kill(s)
    st, p = launch(a, cfg, a.name, prev=s)
    if a.no_wait: return emit(a, {"name": a.name, "pid": st["pid"]})
    dt, _ = wait_agent(st, p, a.timeout)
    emit(a, {"name": a.name, "boot_s": round(dt, 1), "rootfs_build_s": st["build_s"]})


def cmd_probe(a):
    """Show what qh learns about a kernel and which transports/devices it would use."""
    a.kernel = a.kernel or None; a.name = None
    cfg = load_cfg(a); k = cfg["kernel"] or die("no kernel")
    kcfg = kernel_config(k); chan, nic, bus = pick_transport(cfg, kcfg)
    feats = {x: (kcfg or {}).get("CONFIG_" + x, "-") for x in ("VIRTIO_PCI", "VIRTIO_MMIO", "VIRTIO_CONSOLE", "VIRTIO_NET", "VIRTIO_BLK",
             "SERIAL_8250_PCI", "TUN", "DEVTMPFS", "MODULES", "9P_FS", "EXT4_FS", "OVERLAY_FS", "SQUASHFS", "BLK_DEV_INITRD")}
    emit(a, {"kernel": k, "arch": cfg["arch"], "machine": cfg["machine"], "cpu": cfg["cpu"], "config_known": kcfg is not None,
             "chan": chan, "nic": nic, "bus": bus, "modules_shipped": bool(cfg["_mods"]), "features": feats})


def cmd_kernel(a):
    if a.action == "fetch":
        kp, mfs = kernel_fetch(a.arch, a.flavor, a.force); emit(a, {"kernel": kp, "modfs": mfs, "spec": f"alpine:{norm_arch(a.arch)}" + (f":{a.flavor}" if a.flavor else "")})


def cmd_dtb(a):
    """Dump the device tree QEMU generates for the machine (edit with dtc, feed back with --dtb)."""
    cfg = load_cfg(a); out = os.path.abspath(a.out)
    q = [qemu_bin(), "-M", f"{cfg['machine']},dumpdtb={out}", "-cpu", cfg["cpu"], "-m", cfg["mem"], "-smp", str(cfg["smp"]), "-display", "none", "-serial", "null"]
    r = subprocess.run(q, capture_output=True, text=True, creationflags=0x08000000 if os.name == "nt" else 0)
    if not os.path.exists(out): die("dumpdtb failed (machine without a DTB?):\n" + r.stderr[-600:])
    d = open(out, "rb").read()  # QEMU dumps a padded 1 MiB buffer: repack to the real size
    (magic, total, off_st, off_str, off_rsv, ver, lcv, bcpu, sz_str, sz_st) = struct.unpack(">10I", d[:40])
    if magic == 0xD00DFEED:
        rsv = d[off_rsv:off_st]; st_ = d[off_st:off_st + sz_st]; strs = d[off_str:off_str + sz_str]
        n_st = 40 + len(rsv) + (-(40 + len(rsv)) % 8)
        hdr_rsv = bytes(n_st - 40 - len(rsv))
        o_st = n_st; o_str = o_st + len(st_)
        blob = struct.pack(">10I", magic, o_str + len(strs), o_st, o_str, 40, ver, lcv, bcpu, len(strs), len(st_)) + rsv + hdr_rsv + st_ + strs
        open(out, "wb").write(blob)
    emit(a, {"dtb": out, "bytes": os.path.getsize(out)})


def cmd_ls(a):
    rows = []
    if os.path.isdir(STATE):
        for n in sorted(os.listdir(STATE)):
            if os.path.exists(os.path.join(STATE, n, "state.json")):
                s = load(n); rows.append({"name": n, "alive": s["alive"], "pid": s["pid"], "forwards": s["fwd"], "agent_port": s["agent_port"]})
    emit(a, rows)


def cmd_exec(a):
    s = load(a.name, True); ag = agent_for(s)
    req = {"op": "exec", "timeout": a.timeout}
    if a.bg: req["bg"] = True
    if a.cwd: req["cwd"] = a.cwd
    if a.env: req["env"] = dict(e.split("=", 1) for e in a.env)
    if a.argv: req["argv"] = a.cmd
    else: req["cmd"] = " ".join(a.cmd)
    if a.stdin:
        req["stdin"] = base64.b64encode(sys.stdin.buffer.read()).decode()
    r = ag.call(wait=(a.timeout or 3600) + 10, **req); ag.close()
    if not r.get("ok"): die(r.get("error", "exec failed"))
    if a.bg: return emit(a, {"pid": r["pid"], "log": r["log"]})
    out, err = b64d(r.get("out")), b64d(r.get("err"))
    if a.json:
        print(json.dumps({"exit": r["exit"], "stdout": out.decode(errors="replace"), "stderr": err.decode(errors="replace"),
                          "timeout": r["timeout"], "ms": r["ms"]}))
        return
    sys.stdout.buffer.write(out); sys.stdout.flush(); sys.stderr.buffer.write(err); sys.stderr.flush()
    if r["timeout"]: print("qh: command timed out", file=sys.stderr); sys.exit(124)
    sys.exit(r["exit"])


CHUNK = 384 * 1024


def put_file(ag, src, dst):
    mode = stat.S_IMODE(os.stat(src).st_mode)
    if os.name == "nt": mode = 0o755 if open(src, "rb").read(4) in (b"\x7fELF",) or open(src, "rb").read(2) == b"#!" else 0o644
    first = True
    with open(src, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b and not first: break
            r = ag.call(wait=120, op="put", path=dst, mode=mode, append=not first, z=True, data=base64.b64encode(zlib.compress(b, 6)).decode())
            if not r.get("ok"): die(f"put {dst}: {r.get('error')}")
            first = False
            if len(b) < CHUNK: break


def cmd_put(a):
    s = load(a.name, True); ag = agent_for(s); n = 0
    if os.path.isdir(a.src):
        for dp, _, fns in os.walk(a.src):
            for fn in fns:
                full = os.path.join(dp, fn); rel = os.path.relpath(full, a.src).replace("\\", "/")
                put_file(ag, full, a.dst.rstrip("/") + "/" + rel); n += 1
    else:
        dst = a.dst + os.path.basename(a.src) if a.dst.endswith("/") else a.dst
        put_file(ag, a.src, dst); n = 1
    emit(a, {"files": n, "dst": a.dst})


def get_file(ag, src, dst):
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True); off = 0
    with open(dst, "wb") as f:
        while True:
            r = ag.call(wait=120, op="get", path=src, offset=off, len=CHUNK, z=True)
            if not r.get("ok"): die(f"get {src}: {r.get('error')}")
            b = zlib.decompress(b64d(r["data"])) if r.get("z") else b64d(r["data"]); f.write(b); off += len(b)
            if r["eof"] or not b: break


def cmd_get(a):
    s = load(a.name, True); ag = agent_for(s)
    r = ag.call(op="exec", argv=["find", a.src, "-type", "f"] if True else [], timeout=20)
    files = [x for x in b64d(r.get("out")).decode().splitlines() if x]
    if not files: die(f"{a.src}: no such file")
    if len(files) == 1 and files[0] == a.src:
        get_file(ag, a.src, os.path.join(a.dst, os.path.basename(a.src)) if os.path.isdir(a.dst) else a.dst)
    else:
        for fpath in files: get_file(ag, fpath, os.path.join(a.dst, os.path.relpath(fpath, a.src)))
    emit(a, {"files": len(files), "dst": a.dst})


def cmd_logs(a):
    p = os.path.join(sdir(a.name), "console.log")
    if not os.path.exists(p): die("no console log")
    with open(p, errors="replace") as f:
        txt = f.read()
        if a.tail: txt = "\n".join(txt.splitlines()[-a.tail:]) + "\n"
        sys.stdout.write(txt); sys.stdout.flush()
        while a.follow:
            l = f.read()
            if l: sys.stdout.write(l); sys.stdout.flush()
            time.sleep(0.2)


def cmd_console(a):
    """Non-interactive helper: send text to the serial console and print what comes back."""
    s = load(a.name, True); c = socket.create_connection(("127.0.0.1", s["serial_port"]), 3)
    if a.send is not None: c.sendall(a.send.encode().decode("unicode_escape").encode() + b"\n")
    c.settimeout(a.wait); out = b""
    try:
        while True:
            d = c.recv(4096)
            if not d: break
            out += d
    except socket.timeout: pass
    sys.stdout.write(out.decode(errors="replace"))


def qmp(s, execute, **args):
    c = socket.create_connection(("127.0.0.1", s["qmp_port"]), 3); f = c.makefile("rw"); f.readline()
    f.write(json.dumps({"execute": "qmp_capabilities"}) + "\n"); f.flush(); f.readline()
    f.write(json.dumps({"execute": execute, "arguments": args}) + "\n"); f.flush()
    while True:
        r = json.loads(f.readline())
        if "return" in r or "error" in r: c.close(); return r


def cmd_qmp(a):
    s = load(a.name, True); args = json.loads(a.args) if a.args else {}
    print(json.dumps(qmp(s, a.command, **args), indent=1))


def cmd_fwd(a):
    """HOST:GUEST or udp:HOST:GUEST (tcp is the default; HOST 0 = pick a free port)."""
    s = load(a.name, True); parts = a.spec.split(":")
    proto = "tcp"
    if parts[0] in ("tcp", "udp"): proto = parts.pop(0)
    if len(parts) != 2: die("usage: qh fwd [tcp|udp:]HOST:GUEST")
    h, g = parts; hp = int(h) or free_port()
    r = qmp(s, "human-monitor-command", **{"command-line": f"hostfwd_add u0 {proto}::{hp}-:{g}"})
    if r.get("return"): die(r["return"].strip())
    s["fwd"].append(f"{proto}::{hp}-:{g}"); json.dump(s, open(os.path.join(sdir(a.name), "state.json"), "w"), indent=1)
    emit(a, {"host": f"127.0.0.1:{hp}", "guest_port": int(g), "proto": proto})


def emit(a, obj):
    if getattr(a, "json", False) or not isinstance(obj, dict):
        print(json.dumps(obj, indent=None if getattr(a, "json", False) else 1))
    else:
        for k, v in obj.items(): print(f"{k}: {v}")


def main():
    p = argparse.ArgumentParser(prog="qh", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="c", required=True)
    def sp(n, fn, h, name=True):
        q = sub.add_parser(n, help=h); q.set_defaults(fn=fn); q.add_argument("--json", action="store_true")
        if name: q.add_argument("-n", "--name", default=None)
        return q
    def vm_opts(q):
        q.add_argument("--arch", help="aarch64|arm|riscv64|x86_64 (default: detected from the kernel image)")
        q.add_argument("--kernel", help="kernel file, or alpine:<arch>[:flavor] to use a fetched Alpine kernel + modules")
        q.add_argument("--initrd", help="your own initrd instead of the built rootfs (no agent unless it contains one; use --no-wait)")
        q.add_argument("--bios"); q.add_argument("--chan", choices=["auto", "virtio", "pci-serial", "serial"]); q.add_argument("--nic", choices=["auto", "native", "tap", "none"])
        q.add_argument("--bus", choices=["auto", "pci", "mmio"]); q.add_argument("--chan-tty", dest="chan_tty", help="guest tty of the agent UART, e.g. /dev/ttyAMA1")
        q.add_argument("--mem"); q.add_argument("--smp", type=int); q.add_argument("--machine")
        q.add_argument("--cpu"); q.add_argument("--append", help="extra kernel cmdline"); q.add_argument("--net", choices=["static", "dhcp", "none"])
        q.add_argument("--fs", action="append", help="overlay dir merged into rootfs (repeatable)")
        q.add_argument("--pkg", action="append", help="Alpine package to unpack into rootfs (repeatable; deps resolved)")
        q.add_argument("--fwd", action="append", help="host:guest tcp forward, e.g. 8080:80 (0=auto host port)")
        q.add_argument("--disk"); q.add_argument("--dtb"); q.add_argument("--timeout", type=float, default=90)
    q = sp("setup", cmd_setup, "download userland, build agent", name=False); q.add_argument("--force", action="store_true")
    q.add_argument("--arch", default="aarch64", help="aarch64|arm|riscv64|x86_64|all")
    q = sp("probe", cmd_probe, "show kernel features and chosen transports", name=False); vm_opts(q)
    q = sp("kernel", cmd_kernel, "fetch an Alpine kernel (+virtio modules) for an arch", name=False); q.add_argument("action", choices=["fetch"])
    q.add_argument("arch"); q.add_argument("flavor", nargs="?"); q.add_argument("--force", action="store_true")
    q = sp("dtb", cmd_dtb, "dump QEMU's generated device tree", name=False); vm_opts(q); q.add_argument("-o", "--out", default="machine.dtb")
    q = sp("build", cmd_build, "build initramfs only"); vm_opts(q); q.add_argument("--out")
    q = sp("up", cmd_up, "boot a VM"); vm_opts(q); q.add_argument("--restart", action="store_true"); q.add_argument("--no-wait", action="store_true")
    q.add_argument("--dry-run", action="store_true", help="print the QEMU command line and exit")
    q.add_argument("--qemu-arg", dest="extra", action="append", help="raw extra qemu arg (repeatable)")
    q = sp("reload", cmd_reload, "rebuild rootfs + reboot (any VM option can change)"); vm_opts(q)
    q.add_argument("--no-wait", action="store_true"); q.add_argument("--qemu-arg", dest="extra", action="append")
    sp("down", cmd_down, "stop VM"); sp("rm", cmd_rm, "stop + delete state"); sp("ls", cmd_ls, "list VMs", name=False)
    q = sp("exec", cmd_exec, "run command in guest"); q.add_argument("--timeout", type=float, default=60); q.add_argument("--bg", action="store_true")
    q.add_argument("--cwd"); q.add_argument("-e", "--env", action="append"); q.add_argument("--argv", action="store_true", help="no shell, exec argv directly")
    q.add_argument("--stdin", action="store_true"); q.add_argument("cmd", nargs=argparse.REMAINDER)
    q = sp("put", cmd_put, "host -> guest"); q.add_argument("src"); q.add_argument("dst")
    q = sp("get", cmd_get, "guest -> host"); q.add_argument("src"); q.add_argument("dst")
    q = sp("logs", cmd_logs, "serial console log"); q.add_argument("-f", "--follow", action="store_true"); q.add_argument("--tail", type=int)
    q = sp("console", cmd_console, "talk to serial console"); q.add_argument("--send"); q.add_argument("--wait", type=float, default=2)
    q = sp("qmp", cmd_qmp, "raw QMP"); q.add_argument("command"); q.add_argument("args", nargs="?")
    q = sp("fwd", cmd_fwd, "add runtime tcp forward"); q.add_argument("spec")
    q = sub.add_parser("_daemon"); q.set_defaults(fn=lambda a: run_daemon(a.name)); q.add_argument("name")
    a = p.parse_args()
    if getattr(a, "name", 1) is None: a.name = default_name()
    if hasattr(a, "extra") and a.extra is None: a.extra = []
    if a.c == "exec" and a.cmd and a.cmd[0] == "--": a.cmd = a.cmd[1:]
    a.fn(a)


if __name__ == "__main__":
    main()

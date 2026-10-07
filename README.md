# qh — a QEMU harness for AI agents (and humans)

Boot a bare kernel image (or a ready-made Alpine one) in seconds, talk to it through an in-guest bridge agent
(think `qemu-guest-agent`, but tiny and kernel-agnostic), get networking, and rebuild the root filesystem in a fraction of a second.
Built for **IoT / embedded kernel emulation**: aarch64, arm32, riscv64 and x86_64 guests, any machine/DTB/cmdline you throw at it.

* `qh up` → VM ready (agent answering) in ~2–5 s, `qh exec 'cmd'` → stdout/stderr/exit code as JSON.
* Rootfs = Alpine userland + your overlay dirs, packed into an initramfs on the host in ~0.3 s (`qh reload`). No root, no WSL, no mounts.
* Works with **stock kernels that have no virtio-net**: the agent tunnels ethernet over a serial channel into QEMU's user-mode network.
* Ships a **skill** (`skill/SKILL.md`) so Claude Code / Grok / any SKILL.md-aware agent knows how to drive it.
* Windows-first (tested on Windows 10 + QEMU 11), pure Python stdlib on the host, Go only to cross-build the guest agent. Linux/macOS work too.

## Quick start

**Windows - one line** (downloads the harness to `~	ools\qemu-harness`, offers to install Python / QEMU / Go via winget,
builds the guest agent, adds `qh` to your PATH, installs the agent skill for Claude Code and Grok):

```powershell
irm https://raw.githubusercontent.com/b3s3da/qemu-harness/main/install.ps1 | iex
```

Options (`-Yes` = never ask, `-Arch all`, `-Dir`, `-NoDeps`, `-NoPath`, `-NoSkills`, `-Smoke`):

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/b3s3da/qemu-harness/main/install.ps1))) -Yes -Arch all -Smoke
```

Piping a script into `iex` runs it unseen - if you prefer, clone the repo (or download `install.ps1`), read it, and run `.\install.ps1`.
**Linux / macOS**: `git clone ... && ./install.sh` (needs Python 3, `qemu-system-*`, Go).

Then, in any project folder (open a **new** terminal first so PATH is picked up):

```powershell
mkdir lab ; cd lab
qh up --kernel alpine:aarch64          # or --kernel path	o\Image
qh exec 'uname -a; ip -4 -o a show eth0'
qh down
```

Requirements: Python >= 3.8, QEMU (`qemu-system-<arch>` on PATH or `C:\Program Files\qemu`), Go >= 1.22 (for `qh setup`), internet for the first run.
The installer is idempotent: re-running it updates the harness in place and keeps `cache/`.

## Commands

| command | what it does |
|---|---|
| `qh setup [--arch A\|all]` | fetch Alpine minirootfs, build the guest agent (cached in `cache/<arch>`) |
| `qh up [--kernel K] [-n NAME]` | build initramfs, boot, wait for the agent |
| `qh exec [--json] [--timeout N] [--bg] CMD…` | run in the guest; exit code propagated; `--cwd`, `-e K=V`, `--stdin`, `--argv` |
| `qh put SRC DST` / `qh get SRC DST` | copy files or dirs (zlib on the wire; keep them small, see Performance) |
| `qh reload [any VM option]` | rebuild rootfs and relaunch with the same ports; kernel/dtb/mem/cmdline may change |
| `qh fwd [udp:]HOST:GUEST` | add a host→guest TCP/UDP forward at runtime (`0` = pick a free host port) |
| `qh logs [-f] [--tail N]` / `qh console --send CMD` | serial console log / talk to it |
| `qh probe [--kernel K]` | what the kernel supports and which transports `qh` would pick |
| `qh dtb -o m.dtb` | dump QEMU's generated device tree (packed), edit with `dtc`, feed back with `--dtb` |
| `qh kernel fetch ARCH [virt\|lts]` | download an Alpine kernel + virtio/tun/9p modules |
| `qh up --dry-run` | print the exact QEMU command line |
| `qh ls` / `down` / `rm` / `qmp CMD` | lifecycle and raw QMP |

Everything takes `--json`. State lives in `./.qh/<name>/` of the directory you run in (several VMs: `-n NAME`).

## Configuration (`qh.json` in your project, falls back to `qh.example.json`)

```json
{ "kernel": "alpine:aarch64", "mem": "512M", "smp": 2, "fs": ["fs"], "pkgs": ["dropbear", "mosquitto"], "fwd": ["2222:22"] }
```

Every key has a CLI flag (`--arch --kernel --initrd --bios --machine --cpu --mem --smp --dtb --disk --append --net --fs --pkg --fwd --chan --nic --bus --chan-tty --qemu-arg`).
`kernel` may also be `alpine:<arch>[:virt|lts]`.

## Making the guest yours

* **Overlay**: everything under the directories listed in `fs` is copied over the Alpine rootfs (later wins). Put firmware, configs, services there.
  Executable bits are inferred (`#!`, ELF, `bin/`, `sbin/`). `fs/etc/qh.d/rc.local` runs before the VM is declared ready; `qh.init=/path` on the cmdline runs a hook.
* **Packages**: names in `pkgs` are resolved against Alpine's index (deps included) and unpacked on the host — no `apk` needed.
* **Kernel modules**: ship a full tree (`lib/modules/<ver>/modules.dep`, uncompressed `.ko`) and init `modprobe`s virtio/tun/9p; loose `*.ko` + `modules.order` are `insmod`ed.
* The bundled overlay starts dropbear (root, empty password — lab only!), mosquitto, and httpd.

## How it works

```
 qh CLI ──tcp──► per-VM daemon ──tcp──► QEMU chardev ══ virtio-serial / PCI-UART / spare UART ══► qh-agent (Go, in guest)
                    │                                                                                │ exec/put/get
                    └─ ethernet frames ◄──► QEMU "stream" netdev ─ hub ─ slirp (10.0.2.0/24, DNS, hostfwd) ◄── tap eth0 (if no virtio-net)
```

`qh` reads the kernel's embedded config (`/proc/config.gz`) or `<kernel>.config` and picks the best transport: virtio-console + virtio-net
(PCI or MMIO) when present; otherwise the built-in 8250-PCI UART (or a spare board UART via `--chan serial --chan-tty`) with a tap device in the guest.
Kernels without `devtmpfs` are handled (`mdev -s`). The wire protocol is JSON lines (`agent/main.go`), ethernet frames are `F<base64>` lines.

## Performance notes

With virtio-net (Alpine kernels, most distro kernels) the network is normal speed. On a stock Android GKI image (no NIC drivers, virtio as modules)
the tunnel gives roughly 15 KB/s into the guest and 75 KB/s out — fine for scripts, MQTT, HTTP probes; put big payloads in `fs/` and `qh reload` instead.

## Troubleshooting

* `agent did not respond`: `qh logs --tail 80`, then `qh probe`. A kernel with neither virtio-console nor 8250-PCI needs `--chan serial --chan-tty /dev/ttyXX`, or `--initrd X --no-wait` + `qh console`.
* Git Bash rewrites `/tmp/x` arguments into Windows paths — set `MSYS_NO_PATHCONV=1`.
* VMs outlive the launching command: QEMU and the daemon start with `CREATE_BREAKAWAY_FROM_JOB`, falling back to WMI `Win32_Process.Create` when the caller's Job Object forbids breakaway (agent runners / terminals that kill the process tree).
* No flashing console windows: QEMU and the daemon start with `CREATE_NO_WINDOW`.
* aarch64/arm/riscv64 run under TCG on x86 hosts (slow-ish but fine); on a matching host QEMU may use `--qemu-arg "-accel whpx"` / KVM.

## License

GPL-3.0-or-later — see [LICENSE](LICENSE). The guest userland is Alpine Linux (downloaded at setup time, own licenses per package).

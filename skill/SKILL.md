---
name: qemu-harness
description: Boot and drive QEMU guests (aarch64, arm32, riscv64, x86_64 kernels, IoT firmware) through the `qh` CLI - run commands in the guest, copy files, rebuild the rootfs overlay, change kernel/dtb/machine/memory, forward ports, read the serial console. Use when asked to run, test, debug or emulate a kernel or IoT device.
---

`qh` is on PATH (installed by the harness's install script; otherwise `python <harness>/qh.py`).
Run it from the PROJECT folder: VM state goes to `./.qh`, config is `./qh.json` (falls back to the harness's own).
In Git Bash on Windows set `MSYS_NO_PATHCONV=1` so guest paths like `/tmp/x` are not rewritten.

Loop:
1. `qh up --kernel <file>` (arch is detected from the image; `--arch` to force). No kernel of your own? `--kernel alpine:arm` /
   `alpine:riscv64` / `alpine:x86_64` / `alpine:aarch64` fetches a known-good one. First run per arch: `qh setup --arch <a>` (needs Go).
2. `qh exec --json 'cmd'` -> {exit, stdout, stderr}. `--timeout N`; `--bg` for daemons (log path returned).
3. Guest files: put under `fs/` (mirrors guest `/`) then `qh reload` (~3 s). Alpine packages: list in `pkgs` of qh.json (or `--pkg`).
   `put`/`get` are for small files only (slow serial link on kernels without virtio-net).
4. Parameters can change at any time: `qh reload --kernel K --dtb my.dtb --machine M --cpu C --mem 1G --smp 4 --append "..." --qemu-arg "-device ..."`.
   `qh up --dry-run` prints the QEMU command; `qh probe --kernel K` shows detected features/transports; `qh dtb -o m.dtb` dumps QEMU's device tree.
5. Kernel problems: `qh logs --tail 80` (serial console), `qh console --send 'dmesg|tail'`.
6. Services in the default overlay: ssh `-p 2222 root@127.0.0.1` (empty password), MQTT 127.0.0.1:1883, HTTP 127.0.0.1:8080; more via `qh fwd HOST:GUEST`.
7. `qh up` returns once the VM is ready and the VM keeps running after the command (and its job/terminal) exits: no keep-alive loop or background task is needed. `qh fwd [udp:]HOST:GUEST` adds TCP/UDP forwards.
8. `qh down` when finished; `qh ls`; `-n NAME` for several VMs.

Transport is chosen automatically from the kernel's config: virtio-console + virtio-net when available (fast), otherwise the agent
rides a PCI/spare UART and the network is tunnelled (stock Android GKI: ~15 KB/s into the guest). Override with `--chan/--nic/--bus`.
If exec can't reach the agent: `qh ls`, `qh logs`, then `qh probe`. Board without virtio and without a spare UART: `--initrd X --no-wait` + `qh console`.

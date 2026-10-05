// SPDX-License-Identifier: GPL-3.0-or-later
// qh-agent: guest-side bridge for the qemu harness (like qemu-ga, but tiny).
// Speaks line-delimited JSON over a virtio-serial port named "qh.agent".
//
// request : {"id":"..","op":"exec|put|get|ping|poweroff|reboot|sync", ...}
// response: {"id":"..","ok":true, ...} | {"id":"..","ok":false,"error":".."}
// binary payloads are base64 ("out","err","data","stdin").
package main

import (
	"bufio"
	"bytes"
	"compress/zlib"
	"context"
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"
)

type Req struct {
	ID      string            `json:"id"`
	Op      string            `json:"op"`
	Cmd     string            `json:"cmd"`  // run through sh -c
	Argv    []string          `json:"argv"` // or direct exec
	Env     map[string]string `json:"env"`
	Cwd     string            `json:"cwd"`
	Stdin   string            `json:"stdin"` // b64
	Timeout float64           `json:"timeout"`
	BG      bool              `json:"bg"`
	Path    string            `json:"path"`
	Mode    uint32            `json:"mode"`
	Data    string            `json:"data"` // b64
	Append  bool              `json:"append"`
	Offset  int64             `json:"offset"`
	Len     int64             `json:"len"`
	Z       bool              `json:"z"` // payload is zlib-compressed
}

type Resp map[string]any

var (
	wmu sync.Mutex
	out io.Writer
)

func send(r Resp) {
	b, _ := json.Marshal(r)
	b = append(b, '\n')
	wmu.Lock()
	defer wmu.Unlock()
	if out != nil {
		if _, err := out.Write(b); err != nil {
			fmt.Fprintln(os.Stderr, "qh-agent: write:", err)
		}
	}
}

func findPort() string {
	for i := 0; ; i++ {
		ents, _ := filepath.Glob("/sys/class/virtio-ports/*/name")
		for _, e := range ents {
			n, _ := os.ReadFile(e)
			if strings.TrimSpace(string(n)) == "qh.agent" {
				return "/dev/" + filepath.Base(filepath.Dir(e))
			}
		}
		if i > 20 {
			return ""
		}
		time.Sleep(250 * time.Millisecond)
	}
}

// makeRaw puts a tty into raw 8-bit mode (no-op for non-ttys such as virtio ports).
func makeRaw(f *os.File) {
	var t syscall.Termios
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), syscall.TCGETS, uintptr(unsafe.Pointer(&t))); e != 0 {
		return
	}
	t.Iflag &^= syscall.IGNBRK | syscall.BRKINT | syscall.PARMRK | syscall.ISTRIP | syscall.INLCR | syscall.IGNCR | syscall.ICRNL | syscall.IXON | syscall.IXOFF
	t.Oflag &^= syscall.OPOST
	t.Lflag &^= syscall.ECHO | syscall.ECHONL | syscall.ICANON | syscall.ISIG | syscall.IEXTEN
	t.Cflag &^= syscall.CSIZE | syscall.PARENB | 0x100f
	t.Cflag |= syscall.CS8 | syscall.CLOCAL | syscall.CREAD | syscall.B115200
	t.Cc[syscall.VMIN], t.Cc[syscall.VTIME] = 1, 0
	syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), syscall.TCSETS, uintptr(unsafe.Pointer(&t)))
}

var tapFd = -1

// openTap creates a tap interface; ethernet frames are relayed over the channel
// ("F<base64>" lines) to the host daemon, which plugs them into QEMU's slirp.
// Used when the kernel has no NIC driver for any QEMU device (e.g. stock GKI).
func openTap(name string) error {
	fd, err := syscall.Open("/dev/net/tun", syscall.O_RDWR, 0)
	if err != nil {
		return err
	}
	var ifr [40]byte
	copy(ifr[:15], name)
	*(*uint16)(unsafe.Pointer(&ifr[16])) = 0x0002 | 0x1000 // IFF_TAP|IFF_NO_PI
	if _, _, e := syscall.Syscall(syscall.SYS_IOCTL, uintptr(fd), 0x400454ca, uintptr(unsafe.Pointer(&ifr[0]))); e != 0 {
		return e
	}
	tapFd = fd
	go func() {
		buf := make([]byte, 65536)
		for {
			n, err := syscall.Read(fd, buf)
			if err != nil || n <= 0 {
				time.Sleep(10 * time.Millisecond)
				continue
			}
			line := make([]byte, 0, n*4/3+8)
			line = append(line, 'F')
			line = base64.StdEncoding.AppendEncode(line, buf[:n])
			line = append(line, 0x0a)
			wmu.Lock()
			if out != nil {
				out.Write(line)
			}
			wmu.Unlock()
		}
	}()
	return nil
}

// ensureNode creates /dev/<x> from sysfs when the kernel has no devtmpfs and mdev has not run yet.
func ensureNode(dev string) {
	if _, err := os.Stat(dev); err == nil {
		return
	}
	ents, _ := filepath.Glob("/sys/class/*/" + filepath.Base(dev) + "/dev")
	if len(ents) == 0 {
		return
	}
	b, _ := os.ReadFile(ents[0])
	var maj, min int
	fmt.Sscanf(strings.TrimSpace(string(b)), "%d:%d", &maj, &min)
	syscall.Mknod(dev, syscall.S_IFCHR|0o600, (maj&0xfff)<<8|(min&0xff)|((min&^0xff)<<12))
}

func main() {
	chn := flag.String("chan", "auto", "channel device: auto|virtio|/dev/ttyS0")
	tap := flag.String("tap", "", "create tap interface with this name and relay frames over the channel")
	flag.Parse()
	if *tap != "" {
		if err := openTap(*tap); err != nil {
			fmt.Println("qh-agent: tap:", err)
		}
	}
	dev := *chn
	if dev == "auto" || dev == "virtio" {
		dev = findPort()
		if dev == "" && *chn == "auto" {
			dev = "/dev/ttyS0"
		}
	}
	fmt.Println("qh-agent: using", dev)
	for {
		ensureNode(dev)
		f, err := os.OpenFile(dev, os.O_RDWR, 0)
		if err != nil {
			time.Sleep(500 * time.Millisecond)
			continue
		}
		makeRaw(f)
		out = f
		serve(f)
		f.Close()
		out = nil
		time.Sleep(100 * time.Millisecond)
	}
}

// serve reads until the host goes away (read returns EOF while disconnected).
func serve(f *os.File) {
	br := bufio.NewReaderSize(f, 1<<20)
	for {
		line, err := br.ReadBytes(0x0a)
		if len(line) > 1 && line[len(line)-1] == 0x0a {
			switch line[0] {
			case 'F':
				if tapFd >= 0 {
					if fr, e := base64.StdEncoding.DecodeString(strings.TrimSpace(string(line[1:]))); e == nil {
						syscall.Write(tapFd, fr)
					}
				}
			case '{':
				var r Req
				if json.Unmarshal(line, &r) == nil {
					go handle(r)
				}
			}
		}
		if err != nil {
			if err == io.EOF {
				time.Sleep(50 * time.Millisecond) // virtio port: host not connected
			} else {
				return
			}
		}
	}
}

func fail(id string, err error) { send(Resp{"id": id, "ok": false, "error": err.Error()}) }

func handle(r Req) {
	switch r.Op {
	case "ping":
		var u syscall.Sysinfo_t
		syscall.Sysinfo(&u)
		host, _ := os.Hostname()
		send(Resp{"id": r.ID, "ok": true, "uptime": u.Uptime, "hostname": host})
	case "exec":
		doExec(r)
	case "put":
		b, err := base64.StdEncoding.DecodeString(r.Data)
		if err == nil && r.Z {
			var zr io.ReadCloser
			if zr, err = zlib.NewReader(bytes.NewReader(b)); err == nil {
				b, err = io.ReadAll(zr)
			}
		}
		if err != nil {
			fail(r.ID, err)
			return
		}
		if err := os.MkdirAll(filepath.Dir(r.Path), 0o755); err != nil {
			fail(r.ID, err)
			return
		}
		flag := os.O_WRONLY | os.O_CREATE | os.O_TRUNC
		if r.Append {
			flag = os.O_WRONLY | os.O_CREATE | os.O_APPEND
		}
		mode := os.FileMode(r.Mode)
		if mode == 0 {
			mode = 0o644
		}
		f, err := os.OpenFile(r.Path, flag, mode)
		if err != nil {
			fail(r.ID, err)
			return
		}
		_, err = f.Write(b)
		f.Close()
		if err == nil && !r.Append {
			err = os.Chmod(r.Path, mode)
		}
		if err != nil {
			fail(r.ID, err)
			return
		}
		send(Resp{"id": r.ID, "ok": true, "n": len(b)})
	case "get":
		st, err := os.Stat(r.Path)
		if err != nil {
			fail(r.ID, err)
			return
		}
		f, err := os.Open(r.Path)
		if err != nil {
			fail(r.ID, err)
			return
		}
		defer f.Close()
		n := r.Len
		if n <= 0 {
			n = 1 << 20
		}
		buf := make([]byte, n)
		k, err := f.ReadAt(buf, r.Offset)
		if err != nil && err != io.EOF {
			fail(r.ID, err)
			return
		}
		payload := buf[:k]
		if r.Z {
			var zb bytes.Buffer
			zw := zlib.NewWriter(&zb)
			zw.Write(payload)
			zw.Close()
			payload = zb.Bytes()
		}
		send(Resp{"id": r.ID, "ok": true, "size": st.Size(), "mode": uint32(st.Mode().Perm()), "z": r.Z, "n": k,
			"data": base64.StdEncoding.EncodeToString(payload), "eof": r.Offset+int64(k) >= st.Size()})
	case "sync":
		syscall.Sync()
		send(Resp{"id": r.ID, "ok": true})
	case "poweroff", "reboot":
		send(Resp{"id": r.ID, "ok": true})
		syscall.Sync()
		time.Sleep(200 * time.Millisecond)
		cmd := uint(syscall.LINUX_REBOOT_CMD_POWER_OFF)
		if r.Op == "reboot" {
			cmd = syscall.LINUX_REBOOT_CMD_RESTART
		}
		syscall.Reboot(int(cmd))
	default:
		fail(r.ID, fmt.Errorf("unknown op %q", r.Op))
	}
}

func doExec(r Req) {
	var c *exec.Cmd
	ctx := context.Background()
	cancel := func() {}
	if r.Timeout > 0 && !r.BG {
		ctx, cancel = context.WithTimeout(ctx, time.Duration(r.Timeout*float64(time.Second)))
	}
	defer cancel()
	switch {
	case len(r.Argv) > 0:
		c = exec.CommandContext(ctx, r.Argv[0], r.Argv[1:]...)
	case r.Cmd != "":
		c = exec.CommandContext(ctx, "/bin/sh", "-c", r.Cmd)
	default:
		fail(r.ID, fmt.Errorf("exec: no cmd/argv"))
		return
	}
	c.Dir = r.Cwd
	c.Env = os.Environ()
	for k, v := range r.Env {
		c.Env = append(c.Env, k+"="+v)
	}
	c.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	c.Cancel = func() error { return syscall.Kill(-c.Process.Pid, syscall.SIGKILL) }
	c.WaitDelay = time.Second
	if r.Stdin != "" {
		b, _ := base64.StdEncoding.DecodeString(r.Stdin)
		c.Stdin = bytes.NewReader(b)
	}
	if r.BG {
		log := "/tmp/qh-bg-" + r.ID + ".log"
		lf, err := os.Create(log)
		if err != nil {
			fail(r.ID, err)
			return
		}
		c.Stdout, c.Stderr = lf, lf
		if err := c.Start(); err != nil {
			fail(r.ID, err)
			return
		}
		go func() { c.Wait(); lf.Close() }()
		send(Resp{"id": r.ID, "ok": true, "pid": c.Process.Pid, "log": log})
		return
	}
	var so, se bytes.Buffer
	c.Stdout, c.Stderr = &so, &se
	t0 := time.Now()
	err := c.Run()
	code, timedOut := 0, ctx.Err() == context.DeadlineExceeded
	if err != nil {
		if ee, ok := err.(*exec.ExitError); ok {
			code = ee.ExitCode()
		} else {
			fail(r.ID, err)
			return
		}
	}
	send(Resp{"id": r.ID, "ok": true, "exit": code, "timeout": timedOut,
		"out": base64.StdEncoding.EncodeToString(so.Bytes()),
		"err": base64.StdEncoding.EncodeToString(se.Bytes()),
		"ms":  time.Since(t0).Milliseconds()})
}

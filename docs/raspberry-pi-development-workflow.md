# Raspberry Pi Development Workflow

How to launch, deploy to, and debug the two Raspberry Pi targets from the Mac.

Every command below is labelled with where it runs:

| Label | Where |
|-------|-------|
| **[Mac]** | Your MacBook, from the root of `voice-assistant-app` |
| **[Pi 5]** | On the Raspberry Pi 5, over SSH |
| **[Pi Zero 2 W]** | On the Raspberry Pi Zero 2 W, over SSH |

> **Verified vs. assumed.** Sections marked **VERIFIED** were confirmed by
> running the command against real hardware. Sections marked **UNVERIFIED**
> could not be confirmed and are flagged inline. Nothing about the Pi 5's audio
> hardware has been verified.

---

## 1. System architecture

Three repositories, three roles:

```
┌──────────────────────────────────────────┐
│ voice-assistant-app          [Mac]       │
│  - the "brain"                           │
│  - WebSocket SERVER on :8765             │
│  - holds the OpenAI API key              │
│  - session, calibration, parent controls │
└──────────────────────────────────────────┘
                  ▲
                  │ the Pi DIALS OUT to the Mac
                  │ JSON over WebSocket, base64 PCM16 @ 24 kHz mono
                  │
      ┌───────────┴───────────┐
      │                       │
┌─────────────────┐   ┌──────────────────────┐
│ pi5 endpoint    │   │ pizero2w endpoint    │
│ [Pi 5]          │   │ [Pi Zero 2 W]        │
│ mic + speaker   │   │ mic + speaker        │
└─────────────────┘   └──────────────────────┘
                  │
                  ▼
        OpenAI Realtime API
```

**The direction matters.** The app is the *server*; each Pi is a *client* that
dials in. There is no port to poll on a Pi — "is the endpoint ready?" means "did
the endpoint process come up and start dialling the Mac?". The definitive proof
is the `HELLO` handshake arriving at the app.

One target at a time: the Mac has one active Wi-Fi connection, and the two Pis
normally live on different networks.

### Which code runs where

| Runs on | Code |
|---------|------|
| **[Mac]** | `voice-assistant-app` — everything in `src/voice_assistant/`, all scripts in `scripts/` |
| **[Pi 5]** | `voice-assistant-pi5` — audio capture/playback endpoint |
| **[Pi Zero 2 W]** | `voice-assistant-piZero2W` — lightweight audio endpoint |

The Pis are intentionally "dumb": capture audio, send it, play what comes back.
No API keys, no session logic, no model calls.

**Cursor stays on the Mac.** Neither Pi needs Cursor Server, VS Code Server, or a
desktop environment. All remote work happens through `ssh` and `systemd`.

> **Note:** the Pi Zero 2 W currently has `~/.cursor-server` and `~/.cursor`
> directories from earlier work. Nothing in this workflow uses them, and they can
> be removed to reclaim space on the 512 MB board:
> `ssh voice-assistant-pizero2w 'rm -rf ~/.cursor-server ~/.cursor'`

---

## 2. One-time Mac setup

**[Mac]** From the repo root:

```bash
# 1. Python environment (this repo uses venv/, not .venv/)
python3 -m venv venv
./venv/bin/pip install -r requirements.txt

# 2. OpenAI key and app settings
cp .env.example .env
$EDITOR .env          # set OPENAI_API_KEY

# 3. Raspberry Pi target configuration
./scripts/setup-target-config.sh
```

`setup-target-config.sh` creates `config/targets.local.env` (gitignored, mode
600) and lists your saved Wi-Fi networks so you can copy the exact SSID.

### Required SSH aliases

**[Mac]** The launchers never use IP addresses — only aliases from
`~/.ssh/config`:

```sshconfig
Host voice-assistant-pi5
    HostName <pi5-address-or-mdns-name>
    User damianlapenavidal

Host voice-assistant-pizero2w
    HostName voice-assistant-pizero2w.local
    User damianlapenavidal
    IdentityFile ~/.ssh/id_ed25519
    IdentitiesOnly yes
    AddKeysToAgent yes
    UseKeychain yes
```

**Prefer `.local` (mDNS) over a fixed IP, especially for the Pi Zero 2 W.** It
lives on an iPhone hotspot, where DHCP hands out a different address on almost
every reconnect. A hardcoded IP goes stale silently; `.local` follows the device.

> **UNVERIFIED:** the current `voice-assistant-pi5` alias points at a hardcoded
> IP (`172.20.189.253`), which timed out during implementation. Consider changing
> it to a `.local` name for the same reason.

### Passwordless SSH

**[Mac]** Every connection uses `BatchMode=yes` — a password prompt is treated as
a failure, not an invitation. Install your key once per Pi:

```bash
ssh-keygen -t ed25519 -C "mac-to-pi"     # only if you have no key yet
ssh-copy-id voice-assistant-pizero2w
ssh-copy-id voice-assistant-pi5
```

Verify:

```bash
ssh -o BatchMode=yes -o ConnectTimeout=5 voice-assistant-pizero2w true && echo OK
```

### Finding the exact saved Wi-Fi SSID

SSIDs are matched **exactly**. This Mac has both `iPhone de Damian` and
`iPhone de Damián` saved — visually near-identical, different networks to macOS.

**[Mac]**

```bash
./scripts/setup-target-config.sh --list-ssids
```

It marks the currently-joined network and prints every saved name verbatim.
Equivalent raw commands:

```bash
networksetup -listpreferredwirelessnetworks en0     # saved networks
ipconfig getsummary en0 | awk -F' SSID : ' '/ SSID : /{print $2}'   # current
```

> `airport -I` no longer reports the SSID on modern macOS (and the binary is gone
> on macOS 26), which is why `ipconfig getsummary` is used.

---

## 3. Launching a target

**[Mac]**

```bash
./scripts/start-pi5.sh          # Raspberry Pi 5
./scripts/start-pizero2w.sh     # Raspberry Pi Zero 2 W
```

Both are three-line wrappers around one implementation:

```bash
./scripts/start-target.sh pi5
./scripts/start-target.sh pizero2w
```

### Options

| Option | Effect |
|--------|--------|
| `--skip-wifi` | Never change the Mac's Wi-Fi, even if the target is unreachable |
| `--skip-pull` | Do not update the git repository on the Pi |
| `--skip-app` | Prepare the Pi but do not start the local app |
| `--skip-audio-check` | Skip the (silent) ALSA preflight |
| `--logs` | Follow the endpoint journal instead of starting the app |
| `--dry-run` | Print what would happen; change nothing |
| `--help` | Usage |

Anything after `--` is passed to the app:

```bash
./scripts/start-pizero2w.sh -- --web --log-level DEBUG
```

### What a launch does

1. **Load config** — every value from `config/targets.local.env`; a missing
   required key aborts and names the key.
2. **Check reachability first** — `ssh -o BatchMode=yes -o ConnectTimeout=5`.
   **If SSH already works, the Wi-Fi is left alone.**
3. **Switch Wi-Fi only if needed** — joins the target's *saved* network. Never
   handles a password; macOS supplies the saved credential from Keychain.
4. **Verify SSH** — prints remote hostname, OS, kernel, architecture, repo path.
5. **Update the repo** — `git pull --ff-only` (see below).
6. **Audio preflight** — read-only, silent.
7. **Restart the endpoint** — `systemctl --user restart <service>`.
8. **Wait for readiness** — with restart-loop detection.
9. **Start the app** in the foreground with the target selected.
10. **Verify the handshake** — a watchdog reports whether the endpoint actually
    dialled in, and fails loudly with the log command if it did not.

`Ctrl+C` stops the local app and its reverse tunnel cleanly. **It does not stop
the remote service** — that is deliberate. Tear down the full session with:

```bash
./scripts/terminate-pizero2w.sh     # Raspberry Pi Zero 2 W
./scripts/terminate-pi5.sh          # Raspberry Pi 5
# or: ./scripts/stop-target.sh pizero2w
```

That stops the Pi systemd unit, any leftover Mac `voice_assistant` listener on
the target port, and orphaned `ssh -R` tunnels. Safe to re-run when already
stopped.

### `--dry-run` guarantees

A dry run never switches Wi-Fi, never touches a repository, never restarts a
service, never creates or removes remote files, never starts the app, **never
opens an audio device, and never produces sound.** It performs read-only SSH
inspection so it can tell you what *would* fail.

---

## 4. How Git-pull deployment works

Deployment is **non-destructive by contract.**

**[Mac]** the launcher runs, over SSH on the Pi:

```bash
git fetch --prune origin
git checkout <branch>
git pull --ff-only origin <branch>
```

It **never** runs `git reset --hard`, `git clean -fd`, a forced checkout, or a
forced pull. Before pulling it checks `git status --porcelain`; if the remote
worktree has *any* uncommitted or untracked change, deployment **aborts** and
lists the files. Work done directly on a Pi is easy to lose and hard to
reproduce, so the launcher will never discard it for you.

Resolve a dirty worktree yourself, **[Pi]**:

```bash
ssh voice-assistant-pizero2w
cd ~/voice-assistant-piZero2W
git diff                  # review
git stash push -u         # keep it, set it aside
```

`DEPLOY_MODE` exists as a seam for a future `rsync` mode; only `git` is
implemented, and `DEPLOY_MODE=rsync` fails with a clear message.

---

## 5. The endpoint systemd service

### Why a *user* service

`systemctl --user` needs **no sudo at all**, so deployment never requires a
passwordless-sudo grant. The endpoint only needs membership in the `audio` group
to reach `/dev/snd` — **VERIFIED** present on the Pi Zero 2 W. It does not need
root.

A system-level service would only be justified if the endpoint needed privileged
hardware access or had to start before any user session exists. Neither applies.

### Installing it

Templates live in `deploy/systemd/`. **[Pi Zero 2 W]**:

```bash
# From the Mac, copy the template over:
scp deploy/systemd/voice-assistant-pizero2w.service \
    voice-assistant-pizero2w:~/.config/systemd/user/

# Then, on the Pi:
ssh voice-assistant-pizero2w
mkdir -p ~/.config/systemd/user
$EDITOR ~/.config/systemd/user/voice-assistant-pizero2w.service   # set ExecStart
systemctl --user daemon-reload
systemctl --user enable --now voice-assistant-pizero2w
```

> The Pi Zero 2 W template's `ExecStart` targets `ws://127.0.0.1:8765` -- its
> own loopback, never a Mac IP -- because this Pi's iPhone hotspot has been
> observed running IPv6-only NAT64/464XLAT on this iPhone/Mac pairing (see
> "Mac has no routable IPv4" under Troubleshooting). The launcher opens an SSH
> reverse tunnel before each session that forwards this back to the app's real
> listening socket; see `scripts/lib/tunnel.sh` and
> `PIZERO2W_USE_SSH_TUNNEL` in `config/targets.local.env`.

### Lingering — required for a user service

A user service stops when your last session ends, and does not start at boot,
unless lingering is enabled. **VERIFIED:** lingering is currently **disabled**
(`Linger=no`) on the Pi Zero 2 W.

**[Pi Zero 2 W]**

```bash
loginctl enable-linger damianlapenavidal
```

The launcher checks this and warns; it never runs it for you, because it is a
persistent system change.

### Inspecting and viewing logs

**[Mac]**

```bash
# Follow live
ssh voice-assistant-pizero2w 'journalctl --user -u voice-assistant-pizero2w -f --no-pager'

# Or via the launcher
./scripts/start-pizero2w.sh --logs

# Last 50 lines
ssh voice-assistant-pizero2w 'journalctl --user -u voice-assistant-pizero2w --no-pager -n 50'

# Status
ssh voice-assistant-pizero2w 'systemctl --user status voice-assistant-pizero2w --no-pager -l'
```

For a *system*-scope service, drop `--user` and add `sudo`.

The launcher prints the last 50 log lines automatically whenever a restart or
readiness check fails.

---

## 6. Pi Zero 2 W audio — VERIFIED

### Hardware

| Component | Part |
|-----------|------|
| Board | Raspberry Pi Zero 2 W |
| OS | Raspberry Pi OS Lite (Debian 13 trixie), kernel 6.18.34+rpt-rpi-v8, aarch64 |
| Microphone | Adafruit ICS-43434 I²S MEMS |
| Amplifier | Adafruit MAX98357A I²S Class-D |
| Speaker | Mono enclosed 3 W, 4 Ω |

### Verified ALSA configuration

Confirmed on the device with `cat /proc/asound/cards`, `arecord -l`, `aplay -l`,
`aplay -L`:

```text
Card ID            sndrpigooglevoi   (currently index 0; index is NOT stable)
Capture device     plughw:CARD=sndrpigooglevoi,DEV=0
Playback device    plughw:CARD=sndrpigooglevoi,DEV=0
Sample rate        48000 Hz
Capture format     S32_LE
Capture channels   2
Active mic slot    left  (channel 1)
Playback channels  2
```

Provided by `/boot/firmware/config.txt`:

```ini
dtparam=audio=on
dtoverlay=googlevoicehat-soundcard
```

Do not modify the boot audio configuration unless inspection proves it necessary.

### Why the microphone uses the left capture channel

The ICS-43434 is a **mono** microphone with its `SEL` pin tied to GND, which
places it in the **left** slot of a two-channel I²S frame. The right slot carries
nothing.

**VERIFIED by measurement** — a 4-second capture through
`./scripts/audio-diagnostic.sh pizero2w --mic`:

```text
channel 1: Maximum amplitude=0.034626  RMS amplitude=0.001596
channel 2: Maximum amplitude=0.000000  RMS amplitude=0.000000
```

Channel 2 is *exactly* zero. So the endpoint must **capture two channels and
select channel 1**. Recording with `-c 1`, or taking the right slot, yields
silence — and it looks like broken hardware rather than a configuration mistake.

### Why ALSA numeric card indexes must not be hardcoded

**VERIFIED:** this Pi has *two* sound cards:

```text
 0 [sndrpigooglevoi]: RPi-simple - snd_rpi_googlevoicehat_soundcard
 1 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi
```

Index assignment depends on driver probe order. Add a USB audio device, change an
overlay, or hit a different boot race and `hw:0,0` silently becomes the HDMI
output — capture then fails and playback goes somewhere you cannot hear.

Address the card **by ID**, which `aplay -L` confirms is supported:

```text
plughw:CARD=sndrpigooglevoi,DEV=0
```

The preflight resolves the current index for *logging only* and says so.

### Why the speaker pops

The MAX98357A un-mutes when the ALSA playback stream opens and mutes when it
closes. Each transition is an audible pop. **VERIFIED** in the kernel log:

```text
voicehat-codec: Enabling audio amp...
voicehat-codec: Disabling audio amp...
```

This is amplifier behaviour, not a fault.

### Why playback must use one persistent stream

Because each open/close pops, an endpoint that spawns `aplay` per audio chunk
produces a machine-gun of pops and adds process-spawn latency to every chunk on a
512 MB board.

The endpoint should:

- open the playback stream **once per active response** (or per session);
- write chunks into the already-open stream;
- use a short fade-in/fade-out where appropriate;
- close only when playback or the session is finished.

`scripts/audio-diagnostic.sh` follows the same rule: it builds a complete WAV
first, then plays it with a **single** `aplay` invocation — one pop at each end.

### Gain

Two independently configurable values in `config/targets.local.env`:

```bash
PIZERO2W_INPUT_GAIN="1.0"       # microphone
PIZERO2W_PLAYBACK_GAIN="0.35"   # speaker
```

- **Input gain** — the ICS-43434's hardware sensitivity is fixed and its raw
  signal is quiet, so make-up gain happens in software. Use a **steady
  multiplier**. Do **not** normalize realtime chunks individually: per-chunk
  normalization pumps the noise floor up between words and makes level jumps
  audible at every chunk boundary. If more adaptivity is needed, use a slow AGC
  or a limiter with proper attack/release — not per-chunk peak normalization.
- **Playback gain** — `0.35` is the SoX `vol` factor that sounded comfortable
  during manual record-then-play testing. Treat it as a **calibration starting
  point for this speaker**, not a proven realtime PCM multiplier. Keep it low
  enough that peaks do not clip.

Change either value, then re-test:

```bash
./scripts/audio-diagnostic.sh pizero2w --loopback --gain 0.5
```

`--gain` overrides for one run without editing the file.

---

## 7. Safe audio tests

**[Mac]** All modes run the ALSA work on the Pi over SSH.

Deployment, startup, and readiness checks **never produce sound**. Only the two
modes below do, and only when asked for by name.

### Read-only info — silent

```bash
./scripts/audio-diagnostic.sh pizero2w --info
```

Cards, capture/playback devices, the device strings this card supports, the
current index, and any running PCM streams. Opens nothing.

### Microphone test — captures only, plays nothing

```bash
./scripts/audio-diagnostic.sh pizero2w --mic --duration 5
```

Records, then reports per-channel maximum and RMS amplitude. Proves the mic works
*and* that the signal is on the expected channel. **The speaker stays silent.**

### Speaker test — MAKES SOUND

```bash
./scripts/audio-diagnostic.sh pizero2w --speaker
```

Asks for confirmation, then plays a short, **quiet**, fading 440 Hz tone at the
configured playback gain. Finite duration, fade-in and fade-out, one stream open.

> **Keep the speaker away from your ear.** Expect a pop when the stream opens and
> another when it closes. Start quiet and raise gain gradually. **Stop
> immediately (`Ctrl+C`) on harsh continuous noise or distortion.**

No high-amplitude test tone is ever generated, and nothing plays automatically.

### Controlled record-then-play test — MAKES SOUND

```bash
./scripts/audio-diagnostic.sh pizero2w --loopback --duration 5
```

Records, **stops capturing**, then plays the recording back:

```text
capture → stop capture → play response
```

**Half duplex on purpose.** Capture finishes before playback starts, so the
microphone cannot hear the speaker.

### Avoiding microphone-speaker feedback

The mic and speaker sit centimetres apart on one prototyping board with no
acoustic isolation and no echo cancellation. Simultaneous capture and playback
will feed back.

Until the app deliberately supports echo cancellation or the hardware is
physically isolated, keep the half-duplex discipline. The app already implements
the protocol half of this: `MUTE_MIC` before playback, and `UNMUTE_MIC` only
after `PLAYBACK_COMPLETE` (see [protocol.md](protocol.md)).

---

## 8. Troubleshooting

Commands are labelled by where they run.

### Hotspot disabled
The Pi Zero 2 W has nothing to join, so `.local` will not resolve.
**[iPhone]** Settings → Personal Hotspot → on. Keep the phone unlocked and nearby
until the Pi joins, then retry. Hotspots often sleep when no device is attached.

### Incorrect SSID
Symptom: "`<name>` is not a saved network on this Mac", or the join silently
fails. SSIDs match exactly — `URI_Secure` ≠ `URI Secure`, `Damian` ≠ `Damián`.
**[Mac]** `./scripts/setup-target-config.sh --list-ssids`, then correct
`*_WIFI_SSID` in `config/targets.local.env`.

### Pi unreachable
**[Mac]**
```bash
ping -c 3 voice-assistant-pizero2w.local
ssh -v voice-assistant-pizero2w true
```
Check the Pi is powered, on the right network, and that the Mac is on that
network too. For the Pi 5, confirm both machines are on the same VLAN — some
campus networks isolate clients.

### SSH authentication failure
`BatchMode=yes` turns any password prompt into a failure.
**[Mac]** `ssh-copy-id voice-assistant-pizero2w`, then confirm `User` and
`IdentityFile` in `~/.ssh/config`.

### Dirty remote Git repository
Deployment aborts and lists the files — nothing was discarded. **[Pi]** review
with `git diff`, then `git stash push -u` or commit. Re-run the launcher.
Use `--skip-pull` to proceed without deploying.

### GitHub pull failure
Usually no route to GitHub — an iPhone hotspot with no data, or a captive portal.
**[Pi]** `git ls-remote origin`. A diverged branch that cannot fast-forward must
be reconciled manually; the launcher will not force anything.

### Service startup failure
**[Mac]** `ssh <alias> 'systemctl --user status <service> --no-pager -l'`.
The launcher already prints the last 50 journal lines on failure. A unit that
starts and exits immediately almost always means a wrong `ExecStart` or a missing
Python dependency on the Pi.

**VERIFIED:** neither Pi has `websockets`, `numpy`, or `sounddevice` installed
system-wide, and the Pi Zero has no virtual environment. The endpoint needs its
dependencies installed before the service can run.

### Handshake timeout
The service is active but never dialled in. Check the endpoint knows this Mac's
address — the app is the server, the Pi is the client:
**[Mac]** `ipconfig getifaddr en0`, and confirm `lsof -nP -iTCP:8765 -sTCP:LISTEN`
shows the app listening. A Mac IP changes with every network switch.

### Pi hostname or IP changes
Expected on the hotspot. Use `.local` in `~/.ssh/config` rather than an IP.
**[Mac]** `dns-sd -q voice-assistant-pizero2w.local` to check mDNS resolution.

### Mac has no routable IPv4 (SSH reverse tunnel)
On this iPhone/Mac pairing the Personal Hotspot has been observed running
IPv6-only NAT64/464XLAT: `ipconfig getifaddr en0` on the Mac reports `192.0.0.2`
(the standard CLAT placeholder) instead of a real LAN address, and mDNS does not
resolve Mac → Pi over it either. Toggling "Maximize Compatibility" and fully
restarting the hotspot from both ends did **not** fix it -- this may just be how
this pairing behaves.

The fix in place: `PIZERO2W_USE_SSH_TUNNEL="1"` in `config/targets.local.env`
makes the launcher open `ssh -N -R 8765:localhost:8765` before each session
(`scripts/lib/tunnel.sh`), and the endpoint's systemd unit always dials its own
`ws://127.0.0.1:8765` rather than a Mac IP. SSH itself connects reliably
regardless of the hotspot's addressing mode, so this sidesteps IP discovery
entirely. To test the tunnel by hand outside the launcher:

```bash
# [Mac] open and leave running
ssh -N -R 8765:localhost:8765 voice-assistant-pizero2w

# [Pi] point the client at its own loopback
ssh voice-assistant-pizero2w "cd ~/voice-assistant-piZero2W && .venv/bin/python zero2w_client.py ws://127.0.0.1:8765 --debug"
```

If a genuinely routable IPv4 ever becomes available on this network, set
`PIZERO2W_USE_SSH_TUNNEL="0"` and change the unit's `ExecStart` back to a Mac
IP -- but confirm with `ipconfig getifaddr en0` first; a `192.0.0.2` result
means the tunnel is still needed.

### Missing ALSA card
**[Pi]**
```bash
cat /proc/asound/cards
grep -E 'dtoverlay|dtparam=audio' /boot/firmware/config.txt
sudo dmesg | grep -Ei 'google|voicehat|i2s|alsa|snd|audio' | tail -40
```
Expect `dtoverlay=googlevoicehat-soundcard`. A card missing after a config change
usually means a pending reboot, or disconnected I²S wiring (BCLK GPIO18 /
LRCLK GPIO19).

### Capture device unavailable
Something else holds the mic — often a still-running endpoint service.
**[Pi]** `fuser -v /dev/snd/*` and
`grep -l '^state: RUNNING' /proc/asound/card*/pcm*/sub*/status`.
Stop the service and retry.

### Playback device unavailable
Same checks. Also confirm you are addressing the card by ID, not index — with two
cards present, `hw:0,0` may be the HDMI device.

### Wrong microphone channel
Symptom: recording is silent but the device works.
**[Mac]** `./scripts/audio-diagnostic.sh pizero2w --mic` and compare channels.
Channel 1 must carry signal; channel 2 must be ~0. If reversed, `SEL` is wired to
3.3 V rather than GND. Confirm `PIZERO2W_MIC_CHANNEL="left"` and
`PIZERO2W_CAPTURE_CHANNELS="2"`.

### Silent recording (both channels zero)
Work outward:
1. Is the card present, and are you using the right device string? (`--info`)
2. Is `SEL` tied to GND, and `DOUT` on **GPIO20 pin 38** — not GPIO21 pin 40,
   which is the amplifier's `DIN`? These adjacent pins are easy to swap.
3. Is 3.3 V present on the mic's `3V` pin (physical pin 1)?
4. Is the format really `S32_LE`? A 16-bit request can silently truncate.
Raise `PIZERO2W_INPUT_GAIN` only after confirming a signal exists — gain on
silence is still silence.

### Speaker pop with no audio
The stream opened and closed (so the device works) but nothing audible played.
Usually the level is too low. Retry with a higher gain, raising gradually:
```bash
./scripts/audio-diagnostic.sh pizero2w --speaker --gain 0.5
```
Also confirm `VIN` is on **5 V**, not 3.3 V, and that the speaker is wired only
between the amplifier's `+` and `−` terminals — **neither terminal connects to Pi
ground.**

### Audio overrun or underrun
Overrun = capture buffer not drained fast enough. Underrun = playback starved.
On a Pi Zero 2 W these usually mean the endpoint is doing too much per chunk, or
Wi-Fi stalled. The endpoint should log both and use **bounded** buffers — an
unbounded queue converts a transient stall into unbounded latency and then an
out-of-memory kill. **[Mac]** watch for them in the journal.

### Another process holding the ALSA device
**[Pi]** `fuser -v /dev/snd/*`, then stop the offender:
`systemctl --user stop <service>`. The preflight warns when it sees a RUNNING
substream. Note `arecord`/`aplay` left over from a manual test also hold the
device.

---

## 9. Verified vs. assumed — summary

| Claim | Status |
|-------|--------|
| Pi Zero 2 W reachable over SSH; Debian 13, kernel 6.18.34, aarch64 | **VERIFIED** |
| Card `sndrpigooglevoi` present, exposes capture **and** playback | **VERIFIED** |
| `plughw:CARD=sndrpigooglevoi,DEV=0` is valid syntax | **VERIFIED** (`aplay -L`) |
| Capture works at 48 kHz / S32_LE / 2ch | **VERIFIED** (preflight) |
| Left channel carries signal, right is exactly 0.0 | **VERIFIED** (measured) |
| Amplifier pops on stream open/close | **VERIFIED** (kernel log) |
| Two sound cards exist, so index 0 is not stable | **VERIFIED** |
| User is in the `audio` group | **VERIFIED** |
| Lingering is enabled | **VERIFIED** (`loginctl enable-linger` run 2026-07-22) |
| Endpoint repo cloned, systemd service installed + enabled | **VERIFIED** (2026-07-22) |
| Mac hotspot IP is IPv6-only NAT64/CLAT; SSH reverse tunnel required | **VERIFIED** (2026-07-22, see Troubleshooting) |
| Pi Zero lacks `websockets` / `numpy` / `sounddevice` | **VERIFIED** (absent, at initial bring-up) |
| Playback gain `0.35` is comfortable | **ASSUMED** — from earlier manual SoX testing, not re-measured |
| Input gain `1.0` is correct | **ASSUMED** — placeholder; tune by ear |
| Pi 5 audio hardware, ALSA device, channel layout | **UNVERIFIED** — Pi 5 was unreachable; deliberately left blank |
| Pi 5 / Pi Zero endpoint entrypoint commands | **UNVERIFIED** — no endpoint code exists yet |
| Amplifier `GAIN → VIN` low-gain wiring present | **UNVERIFIED** — not physically inspected |

---

## 10. Wiring reference (Pi Zero 2 W)

Documentation only. Nothing in software changes wiring, and none of this was
re-inspected physically.

### ICS-43434 microphone

```text
3V    → 3.3 V, physical pin 1
GND   → GND, physical pin 6
SEL   → GND                      (selects the LEFT slot)
BCLK  → GPIO18, physical pin 12
LRCL  → GPIO19, physical pin 35
DOUT  → GPIO20, physical pin 38
```

### MAX98357A amplifier

```text
VIN   → 5 V, physical pin 2 or 4
GND   → GND, physical pin 6
BCLK  → GPIO18, physical pin 12
LRC   → GPIO19, physical pin 35
DIN   → GPIO21, physical pin 40
```

Shared: `BCLK` (GPIO18), `LRCLK` (GPIO19), `GND`.
Distinct — **do not confuse these adjacent pins**:

```text
Microphone DOUT → GPIO20, pin 38
Amplifier  DIN  → GPIO21, pin 40
```

The speaker connects **only** between the amplifier's `+` and `−` outputs.
**Neither speaker terminal connects to Raspberry Pi ground.**

Recommended low-gain amplifier configuration: `GAIN → VIN`, `SD/MODE`
unconnected. **UNVERIFIED** — not confirmed present.

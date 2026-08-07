# Shipped binaries

## `dropbear-mipsel` / `dropbear-armv7`

The SSH daemon the `ssh` patcher installs onto a rooted PetKit device
(`petkit_local/patchers/ssh.py`). It is served to the device over the patcher
download path and written to persistent app storage (`/system/dropbear` on
Ingenic devices, `/opt/dropbear` on Axera devices).

| | mipsel | armv7 |
|---|---|---|
| Target | `ELF 32-bit LSB PIE, MIPS32 rel2, static` | `ELF 32-bit LSB PIE, ARM EABI5, static` |
| Devices | T5, T6, T7, D4H, D4SH | W7H |
| Size | 165,500 bytes | 128,164 bytes |
| SHA-256 | `cfbc0902dd56a92b74194d5fc07ebceb2b33d8e7037584c6a85ebc4f1e74ee68` | `4651d17f8d11e92928068c29fafb5b20a26d731c35fd8be91a6be34ce2429051` |

| | |
|---|---|
| Upstream | [mkj/dropbear](https://github.com/mkj/dropbear) |
| Version | **2024.86** |
| Licence | MIT-style, plus PuTTY-derived and LibTomCrypt/LibTomMath — see [`LICENSE.dropbear`](LICENSE.dropbear) |
| Compression | UPX 4.2.4 (`--best --lzma`) |
| Cross-compilers | `mipsel-linux-musl-cross` / `armv7l-linux-musleabihf-cross` from [musl.cc](https://musl.cc) |

## How to rebuild

```bash
# Needs Docker. Run from anywhere — output lands in the working directory.
./build-dropbear.sh
```

The script builds both variants in sequence, each in a disposable Debian
container with the musl cross-toolchain. It downloads the dropbear source,
applies the five patches below, compiles, strips, UPX-packs, and prints the
SHA-256 of each output. Takes about two minutes on a modest x86_64 host.

## Source modifications (applied by the build script)

### 1. Hardcoded authorized_keys path

`svr-authpubkey.c`: `"%s/.ssh/authorized_keys"` → `"/tmp/.ssh/authorized_keys"`

Root's home is `/` (read-only squashfs), so `~/.ssh/` does not exist and
cannot be created. `/tmp/.ssh/` is writable (tmpfs). The patcher's boot hook
copies the persistent `authorized_keys` there at startup.

### 2. Buffer size fix

`svr-authpubkey.c`: `len + 22` → `len + 32`

The original allocation assumed a short path. The hardcoded path is 25 chars;
without the fix this is a heap overflow.

### 3. Permission check bypass

`svr-authpubkey.c`: `checkfileperm()` → `return DROPBEAR_SUCCESS` immediately.

Dropbear checks that the authorized_keys file, its parent directory and the
home directory are owned by the user and not world-writable. On the device
`/tmp` is mode 1777, ownership varies, and the checks always fail.

### 4. Password auth disabled

`localoptions.h`: `#define DROPBEAR_SVR_PASSWORD_AUTH 0`

The device's `/etc/shadow` has a DES crypt hash. DES is deprecated and musl's
`crypt()` may not support it. Pubkey-only is also more secure.

### 5. Host key paths moved to /tmp/

```c
#define DSS_PRIV_FILENAME  "/tmp/dbkey_dss"
#define RSA_PRIV_FILENAME  "/tmp/dbkey_rsa"
#define ECDSA_PRIV_FILENAME "/tmp/dbkey_ecdsa"
#define ED25519_PRIV_FILENAME "/tmp/dbkey_ed25519"
```

Fallback paths only — the patcher starts dropbear with
`-r <persistent-storage>/dbkey_ecdsa`, which is persistent across reboots.

## Build configuration

```
./configure --host=<TRIPLE> --disable-zlib --disable-wtmp \
    --disable-lastlog --disable-syslog \
    CC=<TRIPLE>-gcc LDFLAGS="-static -Wl,--gc-sections" \
    CFLAGS="-Os <ARCH_FLAGS> -ffunction-sections -fdata-sections"
```

- **musl static linking** avoids glibc's NSS/dynamic-resolution issues.
- **`-Os` + `--gc-sections`** strips unused code before UPX packs what remains.
- **MULTI=1** produces a multi-call binary: symlinked as `dropbearkey` it
  generates host keys. The patcher uses this to create the ECDSA key on first
  install.
- **Disabled:** zlib (no compression needed on LAN), wtmp/lastlog, syslog.

Dropbear's licence requires its copyright notice to accompany binary
redistributions, which is what `LICENSE.dropbear` is doing here. It is
unaffected by this project's own GPL-3.0 licence.

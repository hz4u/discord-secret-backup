# Third-party notices

Secret is licensed under the GNU General Public License v3.0 or later (see `LICENSE`).
The release `Secret.exe` bundles the components below. Their licenses apply to those parts.

## Libraries

| Component | License | Source |
|---|---|---|
| Python 3.12 | PSF License | https://www.python.org |
| Qt 6 / PySide6 / Shiboken6 | LGPL-3.0 | https://www.qt.io, https://wiki.qt.io/Qt_for_Python |
| FFmpeg (via Qt Multimedia) | LGPL-2.1-or-later | https://ffmpeg.org |
| cryptography | Apache-2.0 or BSD-3-Clause | https://github.com/pyca/cryptography |
| OpenSSL (via cryptography) | Apache-2.0 | https://www.openssl.org |
| httpx, httpcore, idna | BSD-3-Clause | https://github.com/encode/httpx |
| h11, anyio, sniffio | MIT | https://github.com/python-hyper/h11, https://github.com/agronholm/anyio |
| certifi | MPL-2.0 | https://github.com/certifi/python-certifi |
| Pillow | MIT-CMU | https://github.com/python-pillow/Pillow |
| pillow-heif | BSD-3-Clause | https://github.com/bigcat88/pillow_heif |
| libheif, libde265 (via pillow-heif) | LGPL-3.0 | https://github.com/strukturag/libheif, https://github.com/strukturag/libde265 |
| x265 (via pillow-heif) | GPL-2.0-or-later | https://bitbucket.org/multicoreware/x265_git |
| MinGW-w64 runtime (via pillow-heif) | GPL-3.0 with GCC Runtime Library Exception, MIT, BSD | https://www.mingw-w64.org |
| QtAwesome, QtPy | MIT | https://github.com/spyder-ide/qtawesome |
| PyInstaller bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception | https://pyinstaller.org |

## Fonts and icons

| Component | License | Source |
|---|---|---|
| Pretendard | SIL Open Font License 1.1 (`assets/fonts/Pretendard-LICENSE.txt`) | https://github.com/orioncactus/pretendard |
| IBM Plex Mono | SIL Open Font License 1.1 (`assets/fonts/IBMPlex-OFL.txt`) | https://github.com/IBM/plex |
| Phosphor Icons (via QtAwesome) | MIT | https://phosphoricons.com |
| Font Awesome Free brand icon "discord" (via QtAwesome) | CC BY 4.0 | https://fontawesome.com |
| Other icon fonts shipped inside QtAwesome (Font Awesome Free, Material Design Icons, Remix Icon, Elusive Icons, Codicons) | See the QtAwesome package | https://github.com/spyder-ide/qtawesome |
| "Select Multiple" icon (`assets/icons/select-multiple.svg`) from Coolicons by Kryston Schwarze | CC BY 4.0 | https://github.com/krystonschwarze/coolicons |

The source code for every LGPL/GPL component is available at the links above.
The complete source of Secret, including the build script, is in this repository, so you can rebuild the executable with modified versions of these libraries.

Discord is a trademark of Discord Inc. Secret is not affiliated with or endorsed by Discord.

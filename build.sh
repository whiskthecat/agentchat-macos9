#!/usr/bin/env bash
# Build AgentChat for Mac OS 9 as a FAT application (PowerPC + 68K) with Retro68.
#
#   ./build.sh
#
# Apple's Universal Interfaces come from MPW 3.5 Golden Master: set MPW_GM to your copy,
# or MAC_UNIVERSAL_INTERFACES to a folder prepared for Retro68; otherwise it is
# downloaded. CONTAINER selects podman or docker. Output: agentchat/build-fat/AgentChat-fat.bin (MacBinary).
set -euo pipefail
cd "$(dirname "$0")"
# Apple's Universal Interfaces 3.4, from MPW 3.5 Golden Master (a free download from
# Apple in its day, still served by the Internet Archive's copy of Apple's FTP site).
mpw_url=https://web.archive.org/web/20250121125357id_/https://staticky.com/mirrors/ftp.apple.com/developer/Tool_Chest/Core_Mac_OS_Tools/MPW_etc./MPW-GM_Images/MPW-GM.img.bin
mpw_sha256=99bbfa95bb9800c8ffc572fce6d72e561f012331c5c623fa45f732502b6fa872
fetch_mpw() {
  [ -n "${MAC_UNIVERSAL_INTERFACES:-}" ] || [ -n "${MPW_GM:-}" ] && return
  MPW_GM="$PWD/build/downloads/MPW-GM.img.bin"
  if [ ! -f "$MPW_GM" ]; then
    echo "Downloading Apple's MPW 3.5 Golden Master (Universal Interfaces, 25 MB)"
    mkdir -p build/downloads && curl -fsSL "$mpw_url" -o "$MPW_GM.part"
    echo "$mpw_sha256  $MPW_GM.part" | { command -v sha256sum >/dev/null && sha256sum -c - || shasum -a 256 -c -; } >/dev/null
    mv "$MPW_GM.part" "$MPW_GM"
  fi
}
fetch_mpw
engine="${CONTAINER:-$(command -v podman >/dev/null && echo podman || echo docker)}"
image=ghcr.io/autc04/retro68@sha256:cb4d58ea194f81e12e8c9b0735be4be9e65e7ccba58e00490d488af63c3cc33f
args=(--rm -i -v "$PWD/agentchat:/ci/agentchat" -e INTERFACES=universal)
[ -n "${RUN_NAME:-}" ] && args+=(--name "$RUN_NAME")
if [ -n "${MAC_UNIVERSAL_INTERFACES:-}" ]; then
  args+=(-v "$(cd "$MAC_UNIVERSAL_INTERFACES" && pwd):/Retro68-build/toolchain/universal:ro")
else
  args+=(-v "$(cd "$(dirname "$MPW_GM")" && pwd)/$(basename "$MPW_GM"):/mpw/MPW-GM.img.bin:ro"
         -e INTERFACESFILE=/mpw/MPW-GM.img.bin)
fi

"$engine" run "${args[@]}" "$image" /bin/bash -c '
  set -e
  TI=/Retro68-build/toolchain
  cd /ci/agentchat
  rm -rf build-ppc build-m68k build-fat
  mkdir -p build-ppc build-m68k build-fat
  echo "== PowerPC =="
  cmake -S . -B build-ppc -DCMAKE_TOOLCHAIN_FILE=$TI/powerpc-apple-macos/cmake/retroppc.toolchain.cmake >/dev/null
  make -C build-ppc -j"$(nproc)" >/dev/null
  echo "== 68K =="
  cmake -S . -B build-m68k -DCMAKE_TOOLCHAIN_FILE=$TI/m68k-apple-macos/cmake/retro68.toolchain.cmake >/dev/null
  make -C build-m68k -j"$(nproc)" >/dev/null
  echo "== FAT application =="
  $TI/bin/Rez $TI/RIncludes/RetroPPCAPPL.r AgentChat.r -I$TI/RIncludes \
    -DCFRAG_NAME="\"AgentChat\"" --copy build-m68k/AgentChat.code.bin \
    -o build-fat/AgentChat-fat.bin --cc build-fat/AgentChat-fat.dsk --cc build-fat/AgentChat-fat.APPL \
    -t APPL -c AGCH --data build-ppc/AgentChat.pef build-ppc/AgentChat.r.rsrc.bin
  chown -R '"$(id -u):$(id -g)"' build-ppc build-m68k build-fat
'
echo "Built agentchat/build-fat/AgentChat-fat.bin"

# AgentChat

A native Mac OS 9 chat client for today's AI coding agents. The agents can also
use the Mac they're chatting from.

AgentChat is a Classic Mac application written in C against the Mac Toolbox,
using real menus, dialogs, TextEdit, the List Manager, Control Manager scroll bars
and Balloon Help. It talks over Open Transport to a small Python bridge on another
computer on your network. The bridge fronts [OpenCode](https://opencode.ai) and
OpenAI's Codex CLI. Through the bridge, the agent gets about 40 tools that run
natively on the Mac: reading and writing files (both forks, with Finder type and
creator), taking screenshots, listing and launching applications, installing
software from StuffIt and BinHex archives, and inspecting other programs' memory.
You can ask it to install something, debug a crashed program, or edit a
SimpleText file, and watch it happen on the Mac.

It was developed on, and runs on, a 450 MHz iMac G3 with Mac OS 9.2.1.

![AgentChat on Mac OS 9.2.1, talking to Muse Spark through the relay](docs/images/agentchat.png)

## Features

- Streaming replies, conversation history, and a list of model "contacts" (every
  model your OpenCode and Codex accounts offer), each with its last conversation.
- Paste a picture from the clipboard to send it to a vision model. Agents can
  show pictures in the chat too.
- When an agent asks you a question, it appears as a native dialog with choices.
- Tools for agents: files, folders, Finder info, screenshots, processes, windows,
  memory, hardware inventory, Gestalt, launching applications and opening documents,
  importing archives, and Apple-event commands to cooperating applications.
- Self-update from the bridge's file server, keeping your preferences.

## What you need

- **A Mac with Mac OS 9 and Open Transport.** It was tested on an iMac G3 (OS 9.2.1)
  and on QEMU's `mac99` machine (OS 9.2.2). The application is a FAT binary: the
  PowerPC code has been tested; the 68K code builds but hasn't run on real hardware.
- **A computer on the same network that runs Docker** (a Linux box, a NAS, a Mac or a
  PC with Docker Desktop) for the relay.

## Running it

1. **Start the relay** on the Docker computer, in a copy of this repository:

   ```sh
   docker compose up -d
   ```

   It comes with [OpenCode](https://opencode.ai)'s free models, which need no account. New
   conversations start on OpenCode's default free model, and the Mac's model list
   shows the others. The relay uses host networking so the Mac can find it: allow TCP
   and UDP port 8765 through the computer's firewall. On Docker Desktop for Mac or
   Windows, see [Docker Desktop](#docker-desktop).

2. **Install the client.** Download `AgentChat.bin` from the
   [latest release](../../releases/latest) (or [build it](#building)), copy it to the Mac
   and decode it with StuffIt Expander.

3. **Connect.** Launch AgentChat. It finds the relay on your network within a few
   seconds and remembers its address. Start typing.

   To enter an address yourself, choose File → Connection Settings…. Untick **Find relay
   if address fails** to use only that address.

> The agent can change files on your Mac. It writes atomically and won't delete
> folders recursively, but keep backups of anything you care about.

### More models (optional)

Sign the relay in to your own accounts; their models then appear in the Mac's list.

```sh
docker compose exec relay codex login --device-auth   # ChatGPT / OpenAI Codex
docker compose exec relay opencode auth login         # any provider OpenCode supports
docker compose restart
```

Set `AGENT_MODEL` in `compose.yaml` (for example `opencode/big-pickle`) to choose the
model new conversations start with. Sign-ins, history and the agent's working folder
are kept in the `agentchat-data` volume.

Without Docker, run the relay directly with Python 3.12, `pip install Pillow`, `unar`
and OpenCode installed: `python3 relay/start.py`.

### Docker Desktop

Docker Desktop's network doesn't pass the Mac's discovery broadcast through, so start the
relay with ports published instead and enter the computer's address in Connection
Settings:

```sh
docker compose -f compose.yaml -f compose.desktop.yaml up -d
```

Use the same `-f` options with later `docker compose` commands.

### Discovery

The Mac asks for a relay with a UDP broadcast on port 8765, and the relay replies with
its chat port. Discovery doesn't cross routers or VLANs and doesn't authenticate the
relay, so use it on a network you trust; with several relays, enter the one you want.
To turn the relay's side off, set `AGENT_DISCOVERY=0` before `docker compose up -d`.

## Use the Mac from your own coding agent

The relay's Mac tools are also an [MCP](https://modelcontextprotocol.io) server, so a
coding agent on your modern computer (Claude Code, Codex and others) can use the Mac
directly: list and copy files, install software, launch programs, take screenshots.
Register this command as a stdio MCP server, run in the folder with `compose.yaml`
(add `ssh <relay-host>` in front if the relay runs on another machine):

```sh
docker compose exec -T relay python3 /app/bridge/mac_mcp.py
```

For example, with Claude Code:
`claude mcp add agentchat -- docker compose -f /path/to/agentchat/compose.yaml exec -T relay python3 /app/bridge/mac_mcp.py`.
An agent that can't add a server mid-session can call one tool per command:
`docker compose exec -T relay python3 /app/bridge/mac_mcp.py --call imac_list_volumes '{}'`.

The tools read files on the relay's side. To send a file from your computer, copy it into
the container first (`docker compose exec relay mkdir -p /data/transfer`, then
`docker compose cp FILE relay:/data/transfer/`) and import it from `/data/transfer/FILE`.
Ask the agent to start with `imac_read_guide`, which explains Classic Mac files,
resource forks and installing software.

## Building

The client builds with [Retro68](https://github.com/autc04/Retro68) in a container.
It needs Apple's Universal Interfaces from MPW 3.5 Golden Master, which Apple gave away
but which can't be redistributed. `build.sh` downloads that disk image (`MPW-GM.img.bin`)
from the [Internet Archive's copy of Apple's developer FTP site](https://web.archive.org/web/20250121125357/https://staticky.com/mirrors/ftp.apple.com/developer/Tool_Chest/Core_Mac_OS_Tools/MPW_etc./MPW-GM_Images/)
and checks its SHA-256, or uses your own copy if you set `MPW_GM`:

```sh
./build.sh
```

The result is `agentchat/build-fat/AgentChat-fat.bin` (MacBinary).

The bridge's tests use recorded fake backends and never call a real model:

```sh
AGENT_TEST_MODE=1 python3 bridge/test_bridge.py
```

## How it works

- **[docs/protocol.md](docs/protocol.md):** the line-based wire protocol between
  client and bridge, covering prompts, streamed text, history replay, pictures and tool calls.
- **Client** (`agentchat/`): one cooperative event loop. Networking is asynchronous
  Open Transport with bounded send queues, so a slow link never blocks typing. Tool
  requests from the agent run between events using the File Manager, Process
  Manager, QuickDraw and Gestalt.
- **Bridge** (`bridge/agentd.py`): keeps one upstream session per conversation and
  accepts at most one prompt in flight. It rejects anything that isn't a chat line,
  such as an HTTP probe. It also exposes the Mac's tools to the agent as an MCP
  server (`bridge/mac_mcp.py`).
- **Operating guide** (`agent_runtime/`): instructions and skills given to the agent
  about Classic Mac OS, such as resource forks, Finder metadata, installing software
  and debugging, so it doesn't treat the Mac like a Unix box.

## Credits and licence

AgentChat is licensed under the GNU GPL version 3 (see `LICENSE`).
Built with Retro68 by Wolfgang Thaller. The Open Transport setup follows
[ssheven](https://github.com/cy384/ssheven)'s example. Much of the code was written
with AI coding agents, directed and tested on the real machine by a human.

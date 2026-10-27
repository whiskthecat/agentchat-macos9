# Client–bridge protocol

The Mac and the bridge exchange newline-terminated lines over one TCP connection
(port 8765 by default). Text is MacRoman. Inside a payload, a newline is written as
`\n` and a backslash as `\\`. Every line starts with a one-letter verb and a colon.

## Chat

| Direction | Line | Meaning |
| --- | --- | --- |
| Mac → bridge | `P:<text>` | A prompt: the only line that starts inference |
| Mac → bridge | `I:<text>` | An answer to a question the agent asked |
| Mac → bridge | `A:` | Stop the current reply |
| bridge → Mac | `T:<text>` | Streamed reply text |
| bridge → Mac | `S:<status>` | Progress, such as tool use |
| bridge → Mac | `B:<name>` | Name of the speaker for the reply that follows |
| bridge → Mac | `D:` | Reply finished |
| bridge → Mac | `X:<error>` | Failure; for example `X:busy` while a reply is still running |

A connection that opens and sends nothing never creates an upstream session. Each
conversation has at most one prompt in flight. Lines shaped like HTTP requests are
refused with `X:http probes are not chat`.

## Models and conversations

| Line | Meaning |
| --- | --- |
| `F:` | List model contacts; the reply is `F:BEGIN`, `F:ITEM…`, `F:END` |
| `M:<model id>` | Select a contact and reopen its last conversation |
| `Y:<id>\t<name>\t<images 0/1>` | The selected model, and whether it accepts pictures |
| `L:` | List conversations; the reply is `J:BEGIN`, `J:ITEM\t<id>\t<title>`, `J:END`, `K:<active id>` |
| `N:` / `O:<id>` / `R:<title>` / `Z:<id>` | New, open, rename and delete a conversation |

Opening a conversation replays it: `V:BEGIN\t<id>\t<server time>`, then for each
message `V:MESSAGE\t<1 user, 2 assistant>\t<time>\t<speaker>`, `V:TEXT\t<text>` and
`V:DONE`, and finally `V:END`. The client converts the server's clock to the Mac's
own time zone, so replayed times match the menu-bar clock.

## Pictures

The client uploads clipboard pixels as `G:BEGIN\t<asset>\t<w>\t<h>`, a run of
`G:DATA\t<asset>\t<base64 XRGB>` lines and `G:END\t<asset>`. The bridge stores a PNG
and answers `G:READY`. `Q:<asset>` attaches the picture to the next prompt. Pictures
are sent back to the Mac as `G:VIEW`/`G:DATA`/`G:DONE`, reduced to at most 320×240 for
QuickDraw.

## Tools

The agent calls tools through the bridge's MCP server. The bridge sends each call to
the Mac as `C:RPC` with a nonce and a bounded JSON request; the client answers with
`E:` lines carrying the same nonce. Replies from an earlier connection are ignored.
The client handles each request between Toolbox events, so the user interface stays
responsive while the agent works.

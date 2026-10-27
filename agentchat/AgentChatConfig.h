/* compile-time configuration for AgentChat */
#ifndef AGENTCHAT_CONFIG_H
#define AGENTCHAT_CONFIG_H

/* BUMP ON EVERY DEPLOY. Shown in the footer and About so nobody ever has to
 * guess which build is running again (2026-09-29: stale .NEW files caused
 * a wrong-app launch).  Format: small incremental integer, +1 per deploy
 * (was YYYY-MM-DD+letter until 2026-09-29d, then moved to v13 onward).
 * Rendered as "AgentChat v<VER>" by the About string in agentchat.c. */
#define AGENTCHAT_VERSION "98"

/* Empty means find a relay on the local network. Saved settings take priority. */
#define AGENT_HOST  ""
#define AGENT_PORT  8765

/* mini file server carrying self-update builds */
#define HTTP_PORT   8767

#define APP_CREATOR 'AGCH'
#define UPDATE_NAME "\pAgentChat.NEW"

#endif

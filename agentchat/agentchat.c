/*
 * AgentChat — native coding-agent chat terminal for Mac OS 9 (PowerPC).
 *
 * Talks over OpenTransport TCP to the LAN bridge (bridge/agentd.py), which
 * fronts an opencode/strix server. Wire protocol (newline-delimited,
 * backslash-escaped text, MacRoman on the wire):
 *   C->S: <prompt line>
 *   S->C: T:<delta>   S:<status>   D:<end of turn>   X:<error>
 *   C->C: C:<verb>    control from operator: SCREEN | QUIT | UPDATE <file>
 *   C->B: B64M:<w>:<h>:<rowBytes>  B64C:<chunk>  B64E      (screenshot)
 *
 * Build: Retro68 powerpc-apple-macos toolchain (see ../build.sh).
 */

#include <Types.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <Quickdraw.h>
#include <Fonts.h>
#include <Windows.h>
#include <Menus.h>
#include <TextEdit.h>
#include <ControlDefinitions.h>
#include <Appearance.h>
#include <Lists.h>
#include <Icons.h>
#include <Scrap.h>
#include <Dialogs.h>
#include <Balloons.h>
#include <Controls.h>
#include <Devices.h>
#include <DateTimeUtils.h>
#include <OSUtils.h>
#include <StandardFile.h>
#include <Events.h>
#include <Sound.h>
#include <Processes.h>
#include <AppleEvents.h>
#include <Memory.h>
#include <Resources.h>
#include <Files.h>
#include <Folders.h>
#include <QDOffscreen.h>
#include <OpenTransport.h>
#include <OpenTransportProviders.h>

#include "AgentChatConfig.h"
#include "RelayDiscovery.h"
#include "AgentChatHelp.h"
#include "RemoteMac.h"
#include "IdleMetrics.h"
#include "ToolQuestions.h"
#include "ChatImages.h"
#include "ModelSettings.h"
#include <LowMem.h>

#ifdef mc68000
/* The 68K half of the FAT bundle is only a bootstrap; the G3 runs the PPC
 * fragment natively. Apple's m68K OT stubs reference the Shared Library
 * Manager entry + OT client record that this old toolchain lacks — provide
 * link-time stubs. Never executed on PowerPC. */
long __SLM11FuncDispatch(void) { return 0; }
long __gOTClientRecord[64];
#endif

/* inline helpers for network-order conversions (no OT types defined here —
 * InetAddress/InetPort/InetHost come from OpenTransportProviders.h) */
static UInt32 ip_to_nbo(const char *s)
{
    UInt32 parts[4];
    int idx = 0;
    parts[0] = parts[1] = parts[2] = parts[3] = 0;
    while (*s && idx < 4) {
        if (*s >= '0' && *s <= '9') {
            parts[idx] = parts[idx] * 10u + (UInt32)(*s - '0');
            s++;
        } else if (*s == '.') {
            idx++;
            s++;
        } else {
            break;
        }
    }
    if (idx != 3) return 0;
    return (parts[0] << 24) | (parts[1] << 16) | (parts[2] << 8) | parts[3];
}

static UInt16 port_to_nbo(UInt16 p)
{
    /* PPC is BIG-ENDIAN: the numeric value stored in memory already IS the
     * network-order on-wire form — exactly like ip_to_nbo above builds
     * 10.0.2.2 as 0x0A000202 stored as-is.  The old byte-swap here made
     * the app dial 41250 instead of 8865 (VM capture 2026-09-28) and
     * 15650 instead of 8765 on the G3.  No swap. Ever. */
    return p;
}

/* FSpOpenDF open modes (may be missing from these CIncludes) */
#ifndef fsRdCmd
enum { fsRdCmd = 0, fsWrCmd = 1, fsRdWrCmd = 2 };
#endif

/* Chat window (the RIGHT window in the two-window composition, plan §2).
 * Narrowed from the old 640x420 to a 550x500 panel so a ~210px navigation
 * window fits to its left on the 800x600 desktop with margins.  Composer
 * metrics follow plan §6: 24px toolbar band, ~72px white editor, 64px Send. */
static short gChatW = 550, gChatH = 500;
static short gNavW = 210, gNavH = 500;
#define WIN_W      gChatW
#define WIN_H      gChatH
#define TX_X       8
#define TX_Y       8
#define TX_R       (WIN_W - 28)
#define SB_L       (WIN_W - 24)
#define SB_R       (WIN_W - 8)
#define TX_B       (WIN_H - 120)
#define TOOL_T     (TX_B + 8)
#define TOOL_B     (TOOL_T + 24)
#define IN_X       8
#define IN_Y       TOOL_B
#define IN_R       (WIN_W - 80)
#define IN_B       (WIN_H - 24)
#define BTN_L      (WIN_W - 72)
#define BTN_T      (TOOL_T + 1)
#define BTN_R      (WIN_W - 8)
#define BTN_B      (TOOL_T + 23)
#define ST_Y       (WIN_H - 18)
#define NAV_W      gNavW
#define NAV_H      gNavH
#define DRAFT_LIMIT 2048   /* well below TextEdit's 32 KB record limit */
#define PENDING_LIMIT 65536
/* ---- structured-row layout (plan §4/§5).  Text metrics are MEASURED at
 * runtime via GetFontInfo/CharWidth (see gBody* globals), never hardcoded to
 * the point size.  These are the pixel boxes; the line heights are filled from
 * the real font metrics so descenders/ascenders can never clip. ---- */
#define MSG_PAD_X  8     /* left/right padding inside a message band        */
#define MSG_PAD_Y  6     /* vertical padding top/bottom of a message band   */
#define AV_SZ    32      /* framed avatar square (plan: 32x32 + thin frame) */
#define AV_COL   10      /* gap between avatar frame and the text column    */
#define BODY_INDENT (AV_SZ + AV_COL)  /* x of body/name column from msg left */
#define MAXLINES   4000
#define LINEBUF    256

/* ---------------- transcript ring ---------------- */
static char  gLines[MAXLINES][LINEBUF];
static long  gLineCount = 0;
static long  gScroll = 0;
static Boolean gFollow = TRUE;   /* auto-follow the newest message (plan §8) */
static char  gCur[PENDING_LIMIT];
static int   gCurLen = 0;
static char  gStatus[160] = "starting";

/* Per-message presentation metadata alongside the measured line ring.
 *   meta bits: [kind 0=system 1=you 2=agent] | (first<<2) | (tint<<3) */
static unsigned char gLineMeta[MAXLINES];
static char          gLineTime[MAXLINES][8];    /* "HH:MM" or "" */
static short         gLineH[MAXLINES];          /* measured pixel height of each stored row */
/* Separator following a wrapped row: 0=space, 1=paragraph, 2=joined token,
 * 3=end. Retaining it lets resizing reflow without changing the message. */
static unsigned char gLineBreak[MAXLINES];
static int gWrapBreak = 3;
static short gHeadingFont = kFontIDGeneva;
static Style gHeadingFace = bold;

/* Measured text metrics, filled once by init_metrics() from GetFontInfo so the
 * row math uses the REAL font, not the point size (plan §5). */
static int gBodyLineH = 16;   /* Geneva-12 body line advance (ascent+descent+leading) */
static int gBodyAsc   = 11;   /* Geneva-12 ascent (baseline placement) */
static int gNameAsc   = 12;   /* heading/name ascent (Geneva-12 bold) */
static int gSysLineH  = 15;   /* system-notice line advance */
static int gHdrH      = 34;   /* message header row height (>= avatar box) */
/* Pending values set at each message-start site, consumed by flush_cur: */
static int  gPendKind  = 0;   /* 1=You  2=Agent */
static int  gPendFirst = 1;   /* next pushed chat line starts a message */
static int  gPendTint  = 0;   /* row-tint parity for this message */
static char gPendTime[8] = "";
static char gPendName[64] = "Agent";
static unsigned long gMsgSeq = 0;

/* Local wall-clock HH:MM from the Mac clock (LMTime = local secs since
 * 1904). Blank on a dead clock so we simply show no time rather than junk. */
static void now_hhmm(char *out)
{
    unsigned long secs = 0, tod;
    out[0] = 0;
    GetDateTime(&secs);            /* local secs since 1904 (matches menu bar) */
    if (secs == 0) return;         /* clock not set -> show no time */
    tod = secs % 86400UL;
    sprintf(out, "%lu:%02lu", tod / 3600UL, (tod % 3600UL) / 60UL);
}

static long gHistoryTimeDelta = 0;
static Boolean gHistoryTimeSet = FALSE;
/* History carries Unix seconds and a server clock sample. GetDateTime anchors
 * that sample to the Mac's clock, even when its location metadata and clock
 * disagree (as in this VM). The native date service formats archived times.
 * Accept older bridge formats during an in-place upgrade. */
static void history_hhmm(char *out, const char *stamp)
{
    MachineLocation location;
    DateTimeRec date;
    char *end;
    unsigned long seconds;
    long delta;
    out[0] = 0;
    if (!stamp || !stamp[0]) return;
    if (strchr(stamp, ':')) { strncpy(out, stamp, 7); out[7] = 0; return; }
    seconds = strtoul(stamp, &end, 10);
    if (*end || !seconds) return;
    if (gHistoryTimeSet) delta = gHistoryTimeDelta;
    else {
        ReadLocation(&location);
        delta = location.u.gmtDelta & 0x00FFFFFFL;
        if (delta & 0x00800000L) delta -= 0x01000000L;
    }
    SecondsToDate(seconds + 2082844800UL + delta, &date);
    sprintf(out, "%d:%02d", date.hour, date.minute);
}

static WindowPtr  gWin = NULL;
static WindowPtr  gNavWin = NULL;      /* left navigation window (plan §2)     */
static Boolean    gNavVisible = TRUE;  /* nav window open (menu can reopen)    */
static Boolean    gNavDirty = FALSE;   /* nav content needs repaint           */
/* Nav action-strip / conversation-row hit rects (window-local), refreshed by
 * draw_nav() so mouseDown routing has current targets (single source). */
static Rect       gNavStatusRect = {0};
static Rect       gNavNewChatRect = {0};
static Boolean    gDirty = FALSE;   /* transcript/status changed -> repaint */
static Boolean gTranscriptDirty = FALSE, gStatusDirty = FALSE;
static GWorldPtr gTranscriptWorld = NULL;
static unsigned long gTranscriptFrames = 0, gWindowFrames = 0;
static Rect       gTxRect = {0}, gInRect = {0}, gBtnRect = {0};
static Boolean    gComposerFocus = TRUE;
static ListHandle gNavList = NULL;
static ListHandle gModelList = NULL;
#define MODEL_CACHE 256
typedef struct { char id[128], name[64], draft[DRAFT_LIMIT + 1], image[72]; } ModelRecord;
static ModelRecord gModels[MODEL_CACHE];
static short gModelCount = 0, gModelRows = 0, gModelMap[MODEL_CACHE];
static char gModelID[128] = "", gModelName[64] = "Agent";
static Boolean gModelBuilding = FALSE;
static Boolean gModelSupportsImages = FALSE;
static char gDraftImage[72] = "", gNewImage[72] = "";
static ControlHandle gRemoveImageBtn = NULL;
static short gConversationTop = 208;
static Rect gModelRect;
static ControlHandle gReconnectBtn = NULL, gClearBtn = NULL;
static ControlHandle gSettingsBtn = NULL, gStopBtn = NULL;
static Rect gSettingsRect;
static Boolean gHistoryLoading = FALSE, gListBuilding = FALSE, gDraftCached = FALSE;
#define HISTORY_CACHE 128
typedef struct { char id[96], title[128], draft[DRAFT_LIMIT + 1], image[72]; } ChatRecord;
static ChatRecord gChats[HISTORY_CACHE];
static short gChatCount = 0, gNavRows = 1, gNavMap[HISTORY_CACHE + 1];
static char gActiveID[96] = "", gNewDraft[DRAFT_LIMIT + 1] = "";
static long gSelectedMessage = -1, gFindRow = -1;
typedef struct { long row; short offset; } TranscriptPosition;
static TranscriptPosition gTextAnchor = {-1,0}, gTextFocus = {-1,0};
static Boolean gTextDragging;
static const char *copy_row_text(long idx);
static void clear_text_selection(void) { gTextAnchor.row = gTextFocus.row = -1; gTextDragging = FALSE; }
static Boolean has_text_selection(void) {
    long base = gLineCount > MAXLINES ? gLineCount - MAXLINES : 0;
    return gTextAnchor.row >= base && gTextFocus.row >= base &&
        gTextAnchor.row < gLineCount && gTextFocus.row < gLineCount &&
        (gTextAnchor.row != gTextFocus.row || gTextAnchor.offset != gTextFocus.offset);
}
static void text_range(TranscriptPosition *a, TranscriptPosition *b) {
    *a = gTextAnchor; *b = gTextFocus;
    if (a->row > b->row || (a->row == b->row && a->offset > b->offset)) {
        TranscriptPosition swap = *a; *a = *b; *b = swap;
    }
}
static char gFindText[128] = "";
static void history_line(char tag, char *payload);
static void nav_new_chat(void);
static void model_line(char tag, char *payload);
static void request_model(const char *id);
static char *new_draft(void);
static char *new_image(void);
static void cache_draft(void);
static void push_image_row(const char *id, int kind, int tint);
static void image_notice(const char *message);
static void request_conversation(const char *id);
static void connection_settings(void);
static void model_settings(void);
static void rename_conversation(void);
static void delete_conversation(void);
static void find_transcript(Boolean ask);
static void copy_transcript(Boolean selection);
static void save_preferences(void);
static ControlActionUPP gScrollAction = NULL;
static void layout_chat(void);
static void layout_nav(void);
static void reflow_transcript(long oldViewH);
static void arrange_windows(void);
static void update_edit_menu(void);
/* Forward declarations for the scrollbar block below. */
static ControlHandle gScrollBar;
static void redraw(void);
static void draw_transcript(void);
static void paint_changes(void);
static void ask_update(void);
/* Classic scrollbar part codes (Inside Macintosh; stable since 1984). */
enum { sbUp = 20, sbDown = 21, sbPageUp = 22, sbPageDown = 23, sbThumb = 129 };

/* Pixel-based transcript document geometry (plan §8).  gScroll is PIXELS
 * scrolled from the BOTTOM (0 == newest message at bottom, auto-follow).
 * The Control Manager scrollbar value is capped at SCROLL_MAX (16-bit trap),
 * so a taller document is mapped through a linear scale. */
#define SCROLL_MAX 30000
static long gDocHeight = 0;      /* total height of all stored rows (px)     */

static long tx_viewh(void)
{
    long h = (long)(gTxRect.bottom - gTxRect.top);
    return (h > 0) ? h : 1;
}

static long scroll_max(void)
{
    long m = gDocHeight - tx_viewh();
    return (m > 0) ? m : 0;
}

/* map a from-bottom pixel offset -> control value (0 = doc top). */
static long scroll_to_ctl(long sb)
{
    long m = scroll_max();
    if (m <= 0) return 0;
    if (m <= SCROLL_MAX) return m - sb;
    return (long)((double)(m - sb) * SCROLL_MAX / m);
}
/* control value -> from-bottom pixel offset. */
static long ctl_to_scroll(long v)
{
    long m = scroll_max();
    if (m <= 0) return 0;
    if (m <= SCROLL_MAX) return m - v;
    return (long)((double)(m - v) * m / SCROLL_MAX);
}

/* Clamp a from-top scroll offset and update the auto-follow flag: following is
 * only kept while we land on the last pixel of the document. */
static void set_scroll(long s)
{
    long mx = scroll_max();
    if (s < 0) s = 0;
    if (s > mx) s = mx;
    gScroll = s;
    gFollow = (s <= 0);   /* at the bottom (newest) = keep auto-following     */
}

static void sync_scroll(void)
{
    long m, v, view;
    if (gScrollBar == NULL) return;
    m = scroll_max();
    if (gScroll > m) gScroll = m;
    if (gScroll < 0) gScroll = 0;
    v = scroll_to_ctl(gScroll);
    view = tx_viewh();
    if (m > SCROLL_MAX) view = (long)((double)view * SCROLL_MAX / m);
    if (view < 1) view = 1;
    if (GetControlMaximum(gScrollBar) != (short)(m > SCROLL_MAX ? SCROLL_MAX : m))
        SetControlMaximum(gScrollBar, (short)(m > SCROLL_MAX ? SCROLL_MAX : m));
#ifndef mc68000
    /* ControlsLib 8.5+; the FAT app's 68K bootstrap never runs on our PPC target. */
    if (GetControlViewSize(gScrollBar) != view) SetControlViewSize(gScrollBar, view);
#endif
    if (GetControlValue(gScrollBar) != (short)v) SetControlValue(gScrollBar, (short)v);
    if ((**gScrollBar).contrlHilite != (m > 0 ? 0 : 255)) HiliteControl(gScrollBar, m > 0 ? 0 : 255);
}

static pascal void scroll_action(ControlHandle ctl, short part)
{
    GrafPtr saved;
    long v = (long)GetControlValue(ctl);
    long page = (long)(tx_viewh() * 0.9);
    if (page < 24) page = 24;
    switch (part) {
        case sbUp:   v -= 24; break;
        case sbDown: v += 24; break;
        case sbPageUp:   v -= page; break;
        case sbPageDown: v += page; break;
        default: break;   /* thumb: TrackControl already moved the value */
    }
    {
        long mctl = (scroll_max() > SCROLL_MAX) ? SCROLL_MAX : scroll_max();
        if (v < 0) v = 0;
        if (v > mctl) v = mctl;
        /* The live thumb has already changed its value. Leave its tracking
         * state to the Control Manager; repaint only the document here. */
        if (part != sbThumb) SetControlValue(ctl, (short)v);
        set_scroll(ctl_to_scroll(v));
    }
    GetPort(&saved); SetPort(gWin);
    draw_transcript();
    SetPort(saved);
}
/* Composer is now the NATIVE TextEdit Manager (v29).  A single TEHandle is the
 * source of truth for the input: TEKey inserts (and handles delete / replacing
 * a selection), TEClick positions the caret and selection, TEIdle blinks the
 * caret, TEUpdate draws it, and the text we send is read straight from the
 * record (teLength + hText).  This replaces the 2026-09-29 self-drawn gShadow
 * field.  SAFETY against the old "TE corrupted its record" / high-bit style-run
 * poison that used to wedge the cooperative task (and the OS): every byte we
 * hand TEKey is sanitized ASCII — Cmd chords and the burst-typed spurious
 * Option/high-bit junk are dropped upstream in the keyDown handler before it
 * ever reaches TE. */
static TEHandle   gTE = NULL;
static Boolean gSystemScrapOwned = FALSE;
/* Send is a REAL native push button (ControlDefProc 0 = pushButProc), created
 * once and tracked with TrackControl — NOT a hand-drawn rect with a Button()
 * spin.  Per AGENTS.md §"Guiding principle" the Control Manager drives the
 * press-invert / release-restore and its internal tracking loop cannot miss
 * the mouse release, so the old hand-rolled arm/fire state (and the old
 * "Button() wedges" scar it worked around) is gone. */
static ControlHandle gSendBtn = NULL;
static ControlHandle gScrollBar = NULL;
static Boolean    gNeedAgentTag = FALSE;  /* next T: starts a new turn */
static Boolean    gBusy = FALSE;
static short      gThinkingFrame = -1;
static MenuHandle gMenu = NULL;

/* OT state */
static EndpointRef gEP = 0;
static Boolean     gConnected = FALSE;
static Boolean     gAutoConnect = TRUE;
typedef struct {
    UInt32 magic;
    char host[64];
    UInt16 chatPort, updatePort, startup;
    char lastID[96];
} LegacyConnectionPrefs;
typedef struct {
    UInt32 magic;
    char host[64];
    UInt16 chatPort, updatePort, startup;
    char lastID[96];
    char model[128];
} ConnectionPrefs;
static ConnectionPrefs gPrefs = { 'AC02', AGENT_HOST, AGENT_PORT, HTTP_PORT, 1, "", "" };
/* Keep AC02's size/layout and legacy startup=0/1 records unchanged. */
#define PREF_STARTUP 1
#define PREF_DISCOVERY_OFF 2
static Boolean gDiscoverNext = FALSE, gUsingDiscovered = FALSE;
static DiscoveredRelay gDiscovered;
static int         gConnPhase = 0;      /* 0=down 1=async-connecting 2=up */
static UInt32      gConnectStart = 0;
#ifndef mc68000
static volatile UInt32 g_otEvents = 0;
static ProcessSerialNumber gOTProcess;

/* OT async notifier (4-arg OTNotifyUPP signature per os9-agent reference):
 * accumulate event bits; the main loop consumes them. Runs at OT call level. */
static void OTNotifier(void *contextPtr, OTEventCode code,
                       OTResult result, void *cookie)
{
    (void)result; (void)cookie;
    IdleMetricsNotify();
    g_otEvents |= (UInt32)code;
    /* Networking can arrive while another app owns the foreground. Wake our
     * cached owner instead of waiting for the background application's sleep.
     * No drawing, allocation or synchronous I/O runs in this notifier. */
    if (contextPtr) WakeUpProcess((ProcessSerialNumber *)contextPtr);
}
#endif
static UInt32      gLastTry = 0;
static Boolean     gRelayHeartbeat = FALSE;
static Boolean     gRelayGreeted = FALSE;
static UInt32      gRelayLastReceive = 0;
static Boolean gBackground = FALSE;
/* Nested Dialog Manager / AESend waits may consume suspension events before
 * the outer loop sees them. Keep one scheduling state at every event owner. */
static void observe_background_event(const EventRecord *event)
{
    if (event->what == osEvt && (event->message >> 24) == suspendResumeMessage)
        gBackground = !(event->message & resumeFlag);
}

/* wire assembly */
static char  gAccum[65536];
static long  gAccumLen = 0;
/* Preserve a received block's tail while an RPC reply waits for send capacity. */
static char  gRecv[4096];
static long  gRecvLen = 0, gRecvPos = 0;
static Boolean gReceiving = FALSE;
/* send queue */
static char  gQ[65536];
static long  gQLen = 0, gQPos = 0;
static AEEventHandlerUPP gQuitEventUPP = NULL;
static Boolean gDeferredQuit = FALSE;
static short gModalDepth = 0;

/* Remote/native Quit is deliberately conservative: unlike the user's manual
 * Quit commands, unattended callers must never discard an in-memory draft or
 * interrupt a turn. Inspect every cache slot, including an inactive contact. */
static Boolean automatic_quit_allowed(void)
{
    short i;
    if (gBusy || gHistoryLoading || ChatImagesUploading() || ToolQuestionsPending() ||
        ToolQuestionsIsOpen() || ModelSettingsIsOpen() || gModalDepth) return FALSE;
    if ((gTE && (*gTE)->teLength) || gDraftImage[0] || gNewDraft[0] || gNewImage[0]) return FALSE;
    for (i = 0; i < MODEL_CACHE; i++)
        if (gModels[i].draft[0] || gModels[i].image[0]) return FALSE;
    for (i = 0; i < HISTORY_CACHE; i++)
        if (gChats[i].draft[0] || gChats[i].image[0]) return FALSE;
    return TRUE;
}

static pascal OSErr handle_quit_event(const AppleEvent *event, AppleEvent *reply, long refcon)
{
    (void)event; (void)reply; (void)refcon;
    if (!automatic_quit_allowed()) return userCanceledErr;
    /* Return before cleanup so the Apple Event Manager can deliver its reply. */
    gDeferredQuit = TRUE;
    return noErr;
}

static OSErr install_quit_event(void)
{
    OSErr err;
    gQuitEventUPP = NewAEEventHandlerUPP(handle_quit_event);
    if (!gQuitEventUPP) return memFullErr;
    err = AEInstallEventHandler(kCoreEventClass, kAEQuitApplication, gQuitEventUPP, 0, FALSE);
    if (err != noErr) {
        DisposeAEEventHandlerUPP(gQuitEventUPP);
        gQuitEventUPP = NULL;
    }
    return err;
}

static void remove_quit_event(void)
{
    if (gQuitEventUPP) {
        AERemoveEventHandler(kCoreEventClass, kAEQuitApplication, gQuitEventUPP, FALSE);
        DisposeAEEventHandlerUPP(gQuitEventUPP);
        gQuitEventUPP = NULL;
    }
}

static void run_modal_dialog(ModalFilterUPP filter, short *hit)
{
    gModalDepth++;
    ModalDialog(filter, hit);
    gModalDepth--;
}

static void quit_app(void)
{
    remove_quit_event();
    ModelSettingsClose();
    ChatImagesClose();
    RemoteMacReset();
    if (gTranscriptWorld) DisposeGWorld(gTranscriptWorld);
    ExitToShell();
}

static void finish_deferred_quit(void)
{
    if (!gDeferredQuit) return;
    gDeferredQuit = FALSE;
    /* Nested event handling may have changed state after accepting the event.
     * Recheck at the outer-loop boundary rather than ever dropping user data. */
    if (automatic_quit_allowed()) quit_app();
}

/* ---------------- ring helpers ---------------- */
static void push_line_meta(const char *s, int kind, int first, int tint,
                          const char *t)
{
    long i = gLineCount % MAXLINES;
    short h;
    if (kind == 0)        h = (short)gSysLineH;
    else if (first)       h = (short)gHdrH;
    else                  h = (short)gBodyLineH;
    strncpy(gLines[i], s, LINEBUF - 1);
    gLines[i][LINEBUF - 1] = 0;
    gLineMeta[i] = (unsigned char)((kind & 3) | ((first ? 1 : 0) << 2) |
                                  ((tint & 1) << 3));
    if (kind != 0 && first && s && s[0]) {
        TextFont(gHeadingFont); TextSize(12); TextFace(gHeadingFace);
        if (TextWidth(s, 0, (short)strlen(s)) >
            gTxRect.right - gTxRect.left - 2 * MSG_PAD_X - BODY_INDENT - 42)
            h += 12;  /* timestamp gets its own line for a long heading */
        TextFont(kFontIDGeneva); TextSize(12); TextFace(0);
    }
    gLineH[i] = h;
    gLineBreak[i] = 3;
    gLineTime[i][0] = 0;
    if (t && t[0]) { strncpy(gLineTime[i], t, 7); gLineTime[i][7] = 0; }
    gLineCount++;
    /* draw_transcript adjusts the from-bottom offset as content grows. */
    gTranscriptDirty = TRUE;
}

/* System/notice line: full width, muted, no avatar/name/time. */
static void push_line(const char *s)
{
    push_line_meta(s, 0, 1, 0, "");
}

static void push_image_row(const char *id, int kind, int tint)
{
    long idx;
    push_line_meta(id, kind, 0, tint, ""); idx = (gLineCount - 1) % MAXLINES;
    gLineMeta[idx] |= 16;
    gLineH[idx] = ChatImagesHeight(id, gTxRect.right - gTxRect.left - 2 * MSG_PAD_X - BODY_INDENT) + 2 * MSG_PAD_Y;
}

/* Measured-width word wrap (plan §5).  Uses the SAME font as drawing and
 * CharWidth per glyph, so wrapped lines never rely on character counts or a
 * monospaced cell, and the wrap authority is real width, not a 255-char
 * Pascal cap.  Honours hard '\r' breaks and preserves blank lines.  Emits
 * each wrapped line via a callback so the stored path and the live pending
 * path share ONE algorithm. */
static long text_width(const char *s, long n)
{
    long w = 0, i;
    for (i = 0; i < n; i++) w += CharWidth((unsigned char)s[i]);
    return w;
}

typedef void (*wrap_emit)(const char *seg, long len);

static void wrap_emit_lines(const char *body, long avail, wrap_emit cb)
{
    char lb[LINEBUF];
    long li = 0, wsum = 0, brk = -1, k;
    const unsigned char *p = (const unsigned char *)body;
    if (avail < 40) avail = 40;

    TextFont(kFontIDGeneva);
    TextSize(12);
    TextFace(0);

    for (;;) {
        unsigned char c = *p;
        if (c == '\r' || c == '\n' || c == 0) {
            lb[li] = 0;
            gWrapBreak = c == 0 ? 3 : 1;
            cb(lb, li);
            li = 0; wsum = 0; brk = -1;
            if (c == 0) break;
            p++;
            continue;
        }
        int cw = CharWidth(c);
        while (wsum + cw > avail && li > 0) {
            if (brk > 0 && brk < li) {
                lb[brk] = 0;
                gWrapBreak = 0;
                cb(lb, brk);                       /* drop the break space */
                long rem = li - brk - 1;
                memmove(lb, lb + brk + 1, (size_t)rem);
                li = rem;
                wsum = 0;
                for (k = 0; k < rem; k++) wsum += CharWidth((unsigned char)lb[k]);
                brk = -1;
            } else {
                lb[li] = 0;                        /* force-cut long token */
                gWrapBreak = 2;
                cb(lb, li);
                li = 0; wsum = 0; brk = -1;
            }
        }
        lb[li] = (char)c;
        if (c == ' ') brk = li;
        wsum += cw;
        li++;
        p++;
        if (li >= LINEBUF - 1) {                   /* hard safety cut */
            lb[li] = 0;
            gWrapBreak = 2;
            cb(lb, li);
            li = 0; wsum = 0; brk = -1;
        }
    }
}

/* Store-path sink: pushes each wrapped segment as a body row. */
static int  g_ewKind, g_ewTint;
static char g_ewTime[8];
static void store_emit(const char *seg, long len)
{
    char tmp[LINEBUF];
    if (len > LINEBUF - 1) len = LINEBUF - 1;
    memcpy(tmp, seg, (size_t)len);
    tmp[len] = 0;
    push_line_meta(tmp, g_ewKind, 0, g_ewTint, g_ewTime);
    gLineBreak[(gLineCount - 1) % MAXLINES] = (unsigned char)gWrapBreak;
}

static void push_body_wrapped(const char *body, int kind, int tint, const char *t)
{
    long avail = (long)(gTxRect.right - gTxRect.left) - 2 * MSG_PAD_X - BODY_INDENT;
    g_ewKind = kind; g_ewTint = tint;
    g_ewTime[0] = 0;
    if (t && t[0]) { strncpy(g_ewTime, t, 7); g_ewTime[7] = 0; }
    wrap_emit_lines(body, avail, store_emit);
    gLineH[(gLineCount - 1) % MAXLINES] += MSG_PAD_Y;
}

/* Normalize CRLF/LF into Classic CR. Preserve indentation and internal
 * paragraph breaks; omit an entirely whitespace-only message. */
static int normalize_msg(char *s)
{
    char *r = s, *w = s;
    int content = 0;
    while (*r) {
        char c = *r++;
        if (c == '\r') {
            if (*r == '\n') r++;
        } else if (c == '\n') c = '\r';
        if (c != '\r' && c != ' ' && c != '\t') content = 1;
        *w++ = c;
    }
    while (w > s && w[-1] == '\r') w--;
    *w = 0;
    return content ? (int)(w - s) : 0;
}

static void flush_cur(void)
{
    if (gCurLen == 0) return;
    gCur[gCurLen] = 0;
    if (gPendKind == 0) {
        int ln = normalize_msg(gCur);
        if (ln > 0) push_line_meta(gCur, 0, 1, 0, gPendTime);
        gCurLen = 0;
        return;
    }
    /* Strip the transport "You: "/"Agent: " prefix — the header row carries
     * the speaker identity now, not the body text. */
    char *body = gCur;
    if (strncmp(body, "You: ", 5) == 0) body += 5;
    else if (strncmp(body, "Agent: ", 7) == 0) body += 7;
    int blen = normalize_msg(body);
    if (blen <= 0) {                 /* empty/whitespace-only -> drop the
                                      * whole message (no empty header wall) */
        gCurLen = 0;
        return;
    }
    /* One header row (avatar + name + timestamp), then the wrapped body. */
    push_line_meta(gPendKind == 2 ? gPendName : "", gPendKind, 1, gPendTint, gPendTime);
    push_body_wrapped(body, gPendKind, gPendTint, gPendTime);
    gPendFirst = 0;
    gCurLen = 0;
}

static void cur_append_char(char c)
{
    if (gCurLen >= (int)sizeof(gCur) - 2) flush_cur();
    gCur[gCurLen++] = c;
    gTranscriptDirty = TRUE;   /* streaming changes only the transcript */
    /* A turn is one message, even when the body contains newlines. */
}

static void cur_append(const char *s, long n)
{
    while (n-- > 0) cur_append_char(*s++);
}

#ifdef AGENTCHAT_DEV_FIXTURE
/* Clearly-identified development fixture (VMTEST builds only, plan §10/§11).
 * Seeds a representative conversation through the REAL render path so the
 * structured rows, measured wrap and descender handling can be verified in
 * QEMU without a live LLM.  Never compiled into the G3/PROD build. */
/* `name` is stored as the header line's text and drawn by draw_row's header
 * branch; the live/transport path uses emit_message (name == "") which falls
 * back to the kind default "You"/"Agent".  emit_named lets the QA fixture
 * exercise a long sender label.  Both are VMTEST-only. */
static void emit_named(int kind, const char *name, const char *time, const char *body)
{
    int band = (int)(gMsgSeq & 1);   /* alternate band A/B by message (plan §4) */
    gMsgSeq++;
    push_line_meta(name ? name : "", kind, 1, band, time);
    push_body_wrapped(body, kind, band, time);
}

static void emit_message(int kind, const char *time, const char *body)
{
    emit_named(kind, "", time, body);
}

static void seed_fixture(void)
{
    char big[640];
    char tall[3200];
    char bulk[900];
    int k, n, m;

    /* baseline: short alternating + a >255-char long-token force-wrap */
    k = 0;
    k += sprintf(big + k, "jjgqyp descending glyphs wrap nicely down the column and gypqj stays put. ");
    k += sprintf(big + k, "A second sentence with poppy quotes, joyful descenders and jagged tops everywhere. ");
    k += sprintf(big + k, "antidisestablishmentarianism-supercalifragilisticexpialidocious is one unbreakable token that must force-wrap. ");
    k += sprintf(big + k, "And a final trailing line to confirm the block bottom is reachable. ");
    emit_message(1, "9:01", "g j p q y - gypsy jumping quickly");
    emit_message(2, "9:01", "The quick brown fox jumps over the lazy dog.");
    emit_message(1, "9:02", "A paragraph that wraps across several lines and ends with gypqj.");
    emit_message(2, "9:02", big);            /* > 255 chars, long token */

    /* CUMULATIVE HISTORY > 32 KB -- 60 bodies x ~800 chars = ~48 KB total.
     * Placed in the middle so scrolling up reaches a large history while the
     * key visual cases stay near the bottom (reachable by a few screens). */
    for (m = 0; m < 60; m++) {
        k = 0;
        while (k < 760)
            k += sprintf(bulk + k,
                "bulk filler %d grows the cumulative transcript past thirty-two kilobytes to exercise the history limit. ", m);
        emit_message((m & 1) + 1, "9:07", bulk);
    }

    /* Whitespace-heavy body: retain indentation and internal blank lines. */
    gPendKind = 2; gPendFirst = 1;
    gPendTint = (int)(gMsgSeq & 1); gMsgSeq++;
    now_hhmm(gPendTime);
    cur_append("Agent: \r\n\r\n  first para after padded blank.\r\n\r\n\r\n\r\n  second para with big gap before.\r\n\r\n\r\n   \t  \r\n", sizeof("Agent: \r\n\r\n  first para after padded blank.\r\n\r\n\r\n\r\n  second para with big gap before.\r\n\r\n\r\n   \t  \r\n") - 1);
    flush_cur();
    /* WHITESPACE-ONLY AGENT MESSAGE -- "Agent: " with nothing but blanks:
     * must be SKIPPED entirely (no empty header wall). */
    gPendKind = 2; gPendFirst = 1;
    gPendTint = (int)(gMsgSeq & 1); gMsgSeq++;
    now_hhmm(gPendTime);
    cur_append("Agent: \r\n\r\n   \t  \r\n", sizeof("Agent: \r\n\r\n   \t  \r\n") - 1);
    flush_cur();
    /* WHITESPACE-ONLY SYSTEM NOTICE -- likewise skipped (no empty notice row). */
    gPendKind = 0; gPendFirst = 1;
    now_hhmm(gPendTime);
    cur_append("\r\n   \t \r\n", sizeof("\r\n   \t \r\n") - 1);
    flush_cur();
    /* BLANK LINES inside one body (multi-paragraph) -- plan §10 blank-line
     * check; exactly one blank line between paragraphs (now also normalised). */
    gPendKind = 2; gPendFirst = 1;
    gPendTint = (int)(gMsgSeq & 1); gMsgSeq++;
    now_hhmm(gPendTime);
    cur_append("Agent: first paragraph.\r\r", sizeof("Agent: first paragraph.\r\r") - 1);
    cur_append("second paragraph after a blank line.\r\r", sizeof("second paragraph after a blank line.\r\r") - 1);
    cur_append("third paragraph after a second blank line.", sizeof("third paragraph after a second blank line.") - 1);
    flush_cur();
    /* LONG SENDER LABEL -- must not collide with the timestamp or clip */
    emit_named(1, "someone-with-an-extremely-long-handle-name-9000", "9:05",
        "The long handle on the header above must render fully without colliding with the timestamp.");
    /* a SINGLE MESSAGE TALLER than the ~372px viewport (~20 wrapped lines) */
    k = 0;
    for (n = 0; n < 22; n++)
        k += sprintf(tall + k,
            "Line %d of one single very tall message that must exceed the viewport height and stay fully scrollable with no clipping at the block bottom. ", n + 1);
    emit_message(2, "9:06", tall);
    emit_message(1, "9:08", "LAST MESSAGE - bottom of a >32KB history; must be reachable at the scroll bottom.");
}
#endif

static void unescape_append(const char *s, long n)
{
    long i;
    for (i = 0; i < n; i++) {
        char c = s[i];
        if (c == '\\' && i + 1 < n) {
            char d = s[++i];
            if (d == 'n') { cur_append_char('\r'); continue; }
            if (d == 't') { cur_append("  ", sizeof("  ") - 1); continue; }
            c = d;
        }
        cur_append_char(c);
    }
}

/* ---------------- status ---------------- */
static int gTraceRow = 0;
static Boolean gBootTrace = TRUE;   /* only paint the raw boot trace until the
                                     * first full redraw; afterwards the
                                     * transcript + status bar own the screen
                                     * and boot_trace must not smear them. */

/* Immediate on-screen boot trace: paints a C-string via direct QuickDraw into
 * the top of the main window (or a black backing if window not ready). srcCopy
 * so it shows even before any redraw() happens. A hard hang mid-OT-setup thus
 * leaves the last step visible on screen. */
static void boot_trace(const char *msg)
{
    Str255 ps;
    long n = (long)strlen(msg);
    if (n > 254) n = 254;
    ps[0] = (unsigned char)n;
    memcpy(ps + 1, msg, (size_t)n);

    if (gWin == NULL) return;
    if (!gBootTrace) return;   /* transcript owns the screen now */
    /* wrap when the trace fills the visible window */
    if (gTraceRow > (gWin->portRect.bottom - gWin->portRect.top - 30) / 12)
        gTraceRow = 0;
    SetPort(gWin);
    TextFont(kFontIDMonaco);
    TextSize(11);
    TextMode(srcCopy);
    MoveTo((short)(gWin->portRect.left + 5),
           (short)(gWin->portRect.top + 14 + gTraceRow * 12));
    DrawString(ps);
    gTraceRow++;
}

static void update_chat_title(void)
{
    char t[180]; Str255 p; long n;
    if (gModelID[0]) snprintf(t, sizeof t, "Chat - %s", gModelName);
    else strcpy(t, "Chat");
    n = strlen(t); p[0] = (unsigned char)n; memcpy(p + 1, t, n);
    if (gWin) {
        Str255 old;
        GetWTitle(gWin, old);
        if (old[0] != p[0] || memcmp(old + 1, p + 1, p[0])) SetWTitle(gWin, p);
    }
}

static void set_status(const char *msg)
{
    if (!strncmp(gStatus, msg, sizeof gStatus - 1)) return;
    strncpy(gStatus, msg, sizeof(gStatus) - 1);
    gStatus[sizeof(gStatus) - 1] = 0;
    gStatusDirty = TRUE;
    RemoteMacEvent("status", gStatus);
    update_chat_title();
}

/* ---------------- send queue ---------------- */

#include "SendQueue.h"

static void redraw(void);
static void send_line(const char *s);
static void q_flush(void)
{
    while (gConnected && gQPos < gQLen) {
        long n; IdleMetricsSend();
        n = OTSnd(gEP, gQ + gQPos, gQLen - gQPos, 0);
        if (n > 0) { gQPos += n; continue; }
        if (n <= 0) break;   /* no room now; retry next tick */
        break;
    }
    /* Sent bytes consume no queue capacity. Preserve the unsent suffix even
     * after a short write followed by OT flow control or an error. */
    SendQueueCompact(gQ, sizeof(gQ), &gQLen, &gQPos);
}

static Boolean q_put(const char *s, long n)
{
    int result;
    if (!gConnected) return FALSE;
    q_flush();
    result = SendQueueAppend(gQ, sizeof(gQ), &gQLen, &gQPos, s, n);
    if (result != kSendQueueOK) {
        set_status(result == kSendQueueFull ? "send queue full; reply waiting" : "invalid send queue reply");
        return FALSE;
    }
    q_flush();
    return TRUE;
}

static long q_upload_space(void)
{
    long space = (long)sizeof(gQ) - (gQLen - gQPos) - RemoteMacPendingReplyBytes() - 128;
    return space > 0 ? space : 0;
}

static void send_line(const char *s)
{
    long i, n = strlen(s), needed = 1;
    for (i = 0; i < n; i++) needed += (s[i] == '\\' || s[i] == '\r' || s[i] == '\n') ? 2 : 1;
    q_flush();
    if (needed > (long)sizeof gQ - gQLen) { set_status("send queue full; try again"); return; }
    /* Whole wire commands are queued atomically, including large model choices. */
    for (i = 0; i < n; i++) {
        if (s[i] == '\\') { gQ[gQLen++] = '\\'; gQ[gQLen++] = '\\'; }
        else if (s[i] == '\r' || s[i] == '\n') { gQ[gQLen++] = '\\'; gQ[gQLen++] = 'n'; }
        else gQ[gQLen++] = s[i];
    }
    gQ[gQLen++] = '\n'; q_flush();
}

static Boolean valid_address(const char *s)
{
    int count = 0, digits = 0, value = 0;
    while (*s) {
        if (*s >= '0' && *s <= '9') {
            value = value * 10 + *s++ - '0';
            if (++digits > 3 || value > 255) return FALSE;
        } else if (*s++ == '.' && digits && count < 3) {
            count++; digits = value = 0;
        } else return FALSE;
    }
    return count == 3 && digits > 0;
}

static OSErr preferences_spec(FSSpec *spec)
{
    short volume;
    long folder;
    OSErr err = FindFolder(kOnSystemDisk, kPreferencesFolderType, TRUE, &volume, &folder);
    if (err != noErr) return err;
#ifdef AGENTCHAT_DEV_FIXTURE
    return FSMakeFSSpec(volume, folder, "\pAgentChat VMTEST Preferences", spec);
#else
    return FSMakeFSSpec(volume, folder, "\pAgentChat Preferences", spec);
#endif
}

static void load_preferences(void)
{
    ConnectionPrefs prefs;
    LegacyConnectionPrefs old;
    FSSpec file;
    short fd;
    long n = sizeof prefs;
    if (preferences_spec(&file) != noErr || FSpOpenDF(&file, fsRdCmd, &fd) != noErr) return;
    FSRead(fd, &n, &prefs);
    if (n == sizeof old && prefs.magic == 'AC01') {
        memcpy(&old, &prefs, sizeof old);
        prefs = gPrefs;
        memcpy(prefs.host, old.host, sizeof prefs.host);
        memcpy(prefs.lastID, old.lastID, sizeof prefs.lastID);
        prefs.chatPort = old.chatPort; prefs.updatePort = old.updatePort; prefs.startup = old.startup;
        n = sizeof prefs;
    }
    if (n == sizeof prefs && prefs.magic == 'AC02') {
        prefs.host[63] = 0; prefs.lastID[95] = 0;
        prefs.model[127] = 0;
        if ((!prefs.host[0] || valid_address(prefs.host)) && prefs.chatPort && prefs.updatePort) gPrefs = prefs;
    }
    FSClose(fd);
}

static void save_preferences(void)
{
    FSSpec file;
    short fd;
    long n = sizeof gPrefs;
    OSErr err = preferences_spec(&file);
    if (err != noErr && err != fnfErr) { set_status("could not save preferences"); return; }
    err = FSpCreate(&file, APP_CREATOR, 'pref', 0);
    if (err != noErr && err != dupFNErr) { set_status("could not save preferences"); return; }
    if (FSpOpenDF(&file, fsRdWrCmd, &fd) != noErr) { set_status("could not save preferences"); return; }
    err = SetEOF(fd, 0);
    if (err == noErr) err = FSWrite(fd, &n, &gPrefs);
    if (FSClose(fd) != noErr || err != noErr) set_status("could not save preferences");
}

/* ---------------- connection ---------------- */
static void select_relay(const char *host, UInt16 chat, UInt16 update)
{
    if (strcmp(gPrefs.host, host) != 0 || gPrefs.chatPort != chat) {
        char begin[] = "BEGIN", end[] = "END";
        gPrefs.lastID[0] = 0; gActiveID[0] = 0;
        gChatCount = 0; gNewDraft[0] = 0;
        gModelCount = 0; gModelID[0] = gPrefs.model[0] = 0;
        nav_new_chat();
        history_line('J', begin); history_line('J', end);
    }
    strcpy(gPrefs.host, host); gPrefs.chatPort = chat; gPrefs.updatePort = update;
}

static void close_conn(void)
{
    RelayDiscoveryCancel();
    gUsingDiscovered = FALSE;
    ToolQuestionsReset();
    ChatImagesDisconnect();
    RemoteMacReset();
    if (gEP) {
        OTRemoveNotifier(gEP);
        OTSndOrderlyDisconnect(gEP);
        OTCloseProvider(gEP);
        gEP = 0;
    }
    gConnected = FALSE;
    gRelayHeartbeat = FALSE;
    gRelayGreeted = FALSE;
    gRelayLastReceive = 0;
    gBusy = FALSE;
    gDirty = gNavDirty = TRUE;
    gConnPhase = 0;
#ifndef mc68000
    g_otEvents = 0;
#endif
    gQLen = gQPos = gAccumLen = 0;
    gRecvLen = gRecvPos = 0;
    gHistoryLoading = gDraftCached = FALSE;
}

static void try_connect(void)
{
    if (!gAutoConnect) return;
#ifdef mc68000
    /* 68K side never runs the network path (FAT bootstrap only) */
    return;
#else
    char m[160];
    static Boolean ot_inited = FALSE;
    OSStatus st;
    OSStatus err = 0;
    EndpointRef ep;
    TCall call;
    InetAddress addr;
    TBind bindreq;
    const char *host;
    UInt16 chatPort;
    UInt32 now = TickCount();

    if (gConnected) return;

    if (!ot_inited) {
        boot_trace("OT: InitOpenTransport...");
        st = InitOpenTransport();
        if (st != noErr) {
            sprintf(m, "OT init failed err=%ld", st);
            set_status(m);
            boot_trace(m);
            return;
        }
        boot_trace("OT: inited ok");
        ot_inited = TRUE;
    }

    /* Mid-async-connect: do NOT re-issue; wait for the notifier. Timebox it
     * so a black-holed SYN (wrong IP/firewall) retries cleanly. */
    if (gConnPhase == 1) {
        if (now - gConnectStart > 12u * 60u) {
            boot_trace("OT: connect timeout, dropping endpoint");
            if (gEP) { OTCloseProvider(gEP); gEP = 0; }
            gConnPhase = 0;
            gDiscoverNext = TRUE;
            gUsingDiscovered = FALSE;
            set_status("offline (no answer from bridge)");
        }
        return;
    }

    if (RelayDiscoveryActive()) {
        short found = RelayDiscoveryPoll(&gDiscovered);
        if (!found) return;
        gDiscoverNext = FALSE;
        if (found < 0) {
            gLastTry = now;
            set_status("no relay found; check Connection Settings");
            return;
        }
        gUsingDiscovered = TRUE;
    } else {
        UInt32 delay = (!gPrefs.host[0] ? 30u : 5u) * 60u;
        if (gLastTry != 0 && now - gLastTry < delay) return;
        gLastTry = now;
        if ((gDiscoverNext || !gPrefs.host[0]) && !(gPrefs.startup & PREF_DISCOVERY_OFF)) {
            gDiscoverNext = FALSE;
            if (RelayDiscoveryStart(AGENT_PORT)) set_status("finding relay on local network...");
            else {
                sprintf(m, "discovery unavailable (%ld); use Connection Settings", RelayDiscoveryLastError());
                set_status(m);
            }
            return;
        }
        gDiscoverNext = FALSE;
        if (!gPrefs.host[0]) { set_status("set a bridge address or enable discovery"); return; }
    }
    host = gUsingDiscovered ? gDiscovered.host : gPrefs.host;
    chatPort = gUsingDiscovered ? gDiscovered.chatPort : gPrefs.chatPort;

    boot_trace("OT: OTCreateConfiguration+OpenEndpoint...");
    ep = OTOpenEndpoint(OTCreateConfiguration(kTCPName), 0, NULL, &err);
    if (ep == 0 || err != kOTNoError) {
        sprintf(m, "no OT endpoint err=%ld", err);
        set_status(m);
        boot_trace(m);
        return;
    }

    /* Bind synchronously while still in blocking mode (reference os9-agent
     * OTSetup, proven on real iMac): an unbound endpoint answers OTConnect
     * with TOUTSTATE (-3155) — seen on G3, 2026-09-27. Zeroed TBind =
     * any local port, completes before return. */
    boot_trace("OT: bind...");
    OTMemzero(&bindreq, sizeof(bindreq));
    err = OTBind(ep, &bindreq, NULL);
    if (err != kOTNoError) {
        OTCloseProvider(ep);
        sprintf(m, "OTBind err=%ld", err);
        set_status(m); boot_trace(m);
        return;
    }

    /* ORDER MATTERS on real hardware (reference os9-agent, empirical
     * 2026-09-26): OTSetAsynchronous BEFORE OTInstallNotifier.
     * NEVER sync+blocking OTConnect pre-event-loop: on a real G3 it hangs
     * forever without emitting a SYN (observed 2026-09-27 physical test). */
    boot_trace("OT: async + notifier...");
    err = OTSetAsynchronous(ep);
    if (err != kOTNoError) {
        OTCloseProvider(ep);
        sprintf(m, "OTSetAsync err=%ld", err);
        set_status(m); boot_trace(m);
        return;
    }
    err = GetCurrentProcess(&gOTProcess);
    if (err == noErr) err = OTInstallNotifier(ep, NewOTNotifyUPP(OTNotifier), &gOTProcess);
    if (err != kOTNoError) {
        OTCloseProvider(ep);
        sprintf(m, "OTInstallNotifier err=%ld", err);
        set_status(m); boot_trace(m);
        return;
    }

    OTMemzero(&call, sizeof(call));
    OTMemzero(&addr, sizeof(addr));
    OTInitInetAddress(&addr, (InetPort)port_to_nbo(chatPort),
                      (InetHost)ip_to_nbo(host));
    call.addr.buf = (UInt8 *)&addr;
    call.addr.len = call.addr.maxlen = (long)sizeof(addr);

    sprintf(m, "OT: async OTConnect %s ...", host);
    set_status(m);
    boot_trace(m);
    /* Async OTConnect legitimately returns kOTNoDataErr (-3162) meaning
     * "in progress" — completion arrives via T_CONNECT (0x0002). */
    err = OTConnect(ep, &call, NULL);
    if (err != kOTNoError && err != (OSStatus)kOTNoDataErr) {
        OTCloseProvider(ep);
        gDiscoverNext = TRUE;
        gUsingDiscovered = FALSE;
        sprintf(m, "connect err=%ld (bridge down?)", err);
        set_status(m);
        boot_trace(m);
        return;
    }
    gEP = ep;
    gConnPhase = 1;
    gConnectStart = now;
    set_status("connecting (async)...");
#endif
}

/* Consume OT event bits in the main loop (safe thread, events pumping). */
static void pump_ot_events(void)
{
#ifndef mc68000
    UInt32 ev;
    char m[96];
    if (!gEP) return;
    /* Serialize the read/clear with OT's notifier. Plain load/store could
     * erase an arriving T_DISCONNECT/T_CONNECT notification. */
    if (!OTEnterNotifier(gEP)) return;
    ev = g_otEvents;
    g_otEvents = 0;
    OTLeaveNotifier(gEP);

    if (gConnPhase == 1 && (ev & T_CONNECT)) {
        OSStatus cerr = OTRcvConnect(gEP, NULL);
        sprintf(m, "OT: rcvconnect=%ld", (long)cerr);
        boot_trace(m);
        if (cerr == noErr) {
            if (gUsingDiscovered) {
                select_relay(gDiscovered.host, gDiscovered.chatPort, gDiscovered.updatePort);
                save_preferences();
                gUsingDiscovered = FALSE;
            }
            gDiscoverNext = FALSE;
            gConnPhase = 2;
            gConnected = TRUE;
            gRelayLastReceive = TickCount();
            set_status("connected");
            boot_trace("CONNECTED ok");
            flush_cur();
            /* Connection diagnostics live in the footer status strip (plan §4),
             * not as bright text at the top of the transcript, so the host:port
             * notice is intentionally NOT pushed into the message list. */
        } else {
            OTCloseProvider(gEP);
            gEP = 0;
            gConnPhase = 0;
            gDiscoverNext = TRUE;
            gUsingDiscovered = FALSE;
            sprintf(m, "offline (rcvconnect %ld)", (long)cerr);
            set_status(m);
        }
    }
    if ((gConnected || gConnPhase == 1) && (ev & (T_DISCONNECT | T_ORDREL))) {
        Boolean failedConnect = !gConnected;
        if (ev & T_ORDREL) OTRcvOrderlyDisconnect(gEP);
        flush_cur();
        push_line("** connection lost");
        close_conn();
        gDiscoverNext = failedConnect; /* a lost working relay is retried first */
        gConnPhase = 0;
        set_status("connection lost");
    }
#endif
}

/* ---------------- receive / dispatch ---------------- */
static void handle_line(char *line);

#ifndef AGENTCHAT_BINARY_BURST_TICKS
#define AGENTCHAT_BINARY_BURST_TICKS 2u
#endif
static void poll_recv(void)
{
    OTFlags flags;
    long n;
    UInt32 started = TickCount();

    if (!gConnected || gReceiving) return;
    gReceiving = TRUE;
    /* Modal idle loops also call poll_recv. Retry prepared bytes here so a
     * full queue cannot leave a reply waiting until a dialog is dismissed. */
    RemoteMacTick();
    if (RemoteMacBinaryFailed()) close_conn();
    while (gConnected && RemoteMacCanReceive()) {
        if (gRecvPos >= gRecvLen) {
            gRecvLen = gRecvPos = 0;
            /* Read straight into a reusable Memory Manager buffer while in
             * body mode. Never read past its boundary into the next line. */
            n = RemoteMacBinaryRemaining();
            if (n) {
                if (n > 65536L) n = 65536L;
                IdleMetricsReceive();
                n = (long)OTRcv(gEP, RemoteMacBinaryBuffer(), n, &flags);
                if (n == kOTNoDataErr || n == 0) break;
                if (n < 0 || RemoteMacBinaryReceive(RemoteMacBinaryBuffer(), n) != noErr) {
                    close_conn(); set_status("binary import aborted"); break;
                }
                gRelayLastReceive = TickCount();
                /* At most two ticks plus one bounded fork write per burst.
                 * The outer WaitNextEvent(0) yields and services native UI. */
                if ((UInt32)(TickCount() - started) >= AGENTCHAT_BINARY_BURST_TICKS) break;
                continue;
            }
            IdleMetricsReceive();
            n = (long)OTRcv(gEP, gRecv, sizeof(gRecv), &flags);
            if (n == kOTNoDataErr || n == 0) break;
            if (n < 0) {
                flush_cur();
                push_line("** connection lost");
                close_conn();
                set_status("connection lost");
                break;
            }
            gRecvLen = n;
            gRelayLastReceive = TickCount();
        }
        while (gRecvPos < gRecvLen && gConnected && RemoteMacCanReceive()) {
            n = RemoteMacBinaryRemaining();
            if (n) {
                if (n > gRecvLen - gRecvPos) n = gRecvLen - gRecvPos;
                if (RemoteMacBinaryReceive(gRecv + gRecvPos, n) != noErr) {
                    close_conn(); set_status("binary import aborted"); break;
                }
                gRecvPos += n;
                continue;
            }
            char ch = gRecv[gRecvPos++];
            if (ch == '\n' || ch == '\r') {
                if (gAccumLen > 0) {
                    gAccum[gAccumLen] = 0;
                    if (!strncmp(gAccum, "B:", 2)) {
                        /* Binary headers use exactly LF. Accepting CRLF here
                         * would turn its LF into the first body byte. */
                        if (ch != '\n' || RemoteMacBinaryBegin(gAccum) != noErr) {
                            close_conn(); set_status("invalid binary import frame");
                        }
                    } else handle_line(gAccum);
                    gAccumLen = 0;
                }
            } else if (gAccumLen < (long)sizeof(gAccum) - 1) {
                gAccum[gAccumLen++] = ch;
            }
        }
    }
    gReceiving = FALSE;
    /* Drain received bytes before checking time: a busy cooperative Mac may
     * have queued heartbeats. Legacy relays never arm this watchdog. No new
     * timer/notifier or shorter idle sleep is needed. UInt32 handles wrap. */
    if (gConnected && (gRelayHeartbeat || !gRelayGreeted) &&
        (UInt32)(TickCount() - gRelayLastReceive) >= 45u * 60u) {
        flush_cur();
        push_line("** relay stopped responding; reconnecting");
        close_conn();
        set_status("relay timeout; reconnecting");
    }
}

/* ---------------- screenshot ---------------- */
static const char kB64[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static long b64_encode(const unsigned char *in, long len, char *out)
{
    long o = 0, i;
    for (i = 0; i < len; i += 3) {
        long v = (long)in[i] << 16;
        if (i + 1 < len) v |= (long)in[i + 1] << 8;
        if (i + 2 < len) v |= (long)in[i + 2];
        out[o++] = kB64[(v >> 18) & 63];
        out[o++] = kB64[(v >> 12) & 63];
        out[o++] = (i + 1 < len) ? kB64[(v >> 6) & 63] : '=';
        out[o++] = (i + 2 < len) ? kB64[v & 63] : '=';
    }
    out[o] = 0;
    return o;
}

static void do_screen(void)
{
    GWorldPtr gw = NULL;
    PixMapHandle pm;
    Ptr base;
    CGrafPtr savePort;
    GDHandle saveGD;
    Rect scr;
    long w, h, rowBytes, total;
    char meta[64];
    char *b64;
    long n, pos;
    char chunk[248];

    scr = qd.screenBits.bounds;
    w = scr.right - scr.left;
    h = scr.bottom - scr.top;

    if (NewGWorld(&gw, 1, &scr, NULL, NULL, 0) != noErr) {
        send_line("B64E");
        return;
    }
    pm = GetGWorldPixMap(gw);
    GetGWorld(&savePort, &saveGD);
    SetGWorld(gw, NULL);
    CopyBits((BitMap *)&qd.screenBits, (BitMap *)*pm, &scr, &scr, srcCopy, NULL);
    SetGWorld(savePort, saveGD);

    rowBytes = (long)((*pm)->rowBytes & 0x7FFF);
    total = rowBytes * h;

    sprintf(meta, "B64M:%ld:%ld:%ld", w, h, rowBytes);
    send_line(meta);

    /* Lock the PIXEL store (not just the PixMap handle) and fetch the base
     * address only AFTER all allocations: NewPtr can compact the heap and
     * move unlocked pixel memory, leaving a stale base that reads heap
     * garbage for later rows (seen 2026-09-28: top ~100 rows image, rest
     * ASCII heap text). */
    b64 = NewPtr((long)(total / 3 + 1) * 4 + 32);
    if (b64 != NULL && LockPixels(pm)) {
        base = GetPixBaseAddr(pm);
        n = b64_encode((const unsigned char *)base, total, b64);
        UnlockPixels(pm);
        pos = 0;
        while (pos < n) {
            /* Prefix every chunk: the bridge only accumulates B64C: lines
             * (bare base64 was dropped as "non-prompt", 2026-09-28). */
            long take = (n - pos > 240) ? 240 : (n - pos);
            memcpy(chunk + 5, b64 + pos, take);
            memcpy(chunk, "B64C:", 5);
            chunk[take + 5] = 0;
            send_line(chunk);
            pos += take;
        }
        DisposePtr(b64);
    }
    send_line("B64E");
    DisposeGWorld(gw);
}

/* ---------------- self-update ---------------- */
static void fail_update(const char *why)
{
    push_line(why);
    set_status("update failed");
}

/* ---------------- async HTTP engine ----------------
 * NEVER use OTSetSynchronous+OTSetBlocking here: a blocking OTConnect/OTRcv
 * with no pump HARD-LOCKS all of Mac OS 9 (verified in VM 2026-09-28: whole
 * system frozen, clock stopped, QMP system_reset could not fire because the
 * guest spun with MSR.EE=0).  Async connect + notifier + pumped waits only. */

typedef struct {
    volatile UInt32   bits;
    volatile OSStatus connResult;
} HttpEv;

static HttpEv g_hev;

static void HttpNotifier(void *contextPtr, OTEventCode code,
                        OTResult result, void *cookie)
{
    (void)contextPtr; (void)cookie;
    if (code == T_CONNECT) g_hev.connResult = result;
    g_hev.bits |= (UInt32)code;
}

static void http_pump(void)
{
    EventRecord ev;
    WaitNextEvent(everyEvent, &ev, 2, NULL);
    observe_background_event(&ev);
}

/* Wait for one OT event bit with a hard timeout. Consumes the bit. */
static Boolean http_wait_bits(UInt32 want, UInt32 timeoutTicks)
{
    UInt32 t = TickCount();
    while ((g_hev.bits & want) == 0) {
        http_pump();
        if ((g_hev.bits & want) != 0) break;
        if ((TickCount() - t) > timeoutTicks) return FALSE;
    }
    g_hev.bits &= ~want;
    return TRUE;
}

/* Perform the GET and write the body into an already-open fork fd. */
static Boolean http_fetch(const char *host, int port, const char *path,
                         SInt16 forkFd)
{
    OSStatus err;
    EndpointRef ep;
    TCall call;
    InetAddress addr;
    TBind b;
    char req[256];
    /* Header buffer MUST exceed the largest single OTRcv (8192): the first
     * recv of a big file holds the header plus kilobytes of body, and any
     * bytes past this buffer used to be silently dropped — yielding a
     * short-but-"complete" file (82,568 of 88,713 bytes, 2026-09-28). */
    char hdr[9000];
    long hdrLen = 0, contentLen = -1, total = 0;
    char buf[8192];
    Boolean headerDone = FALSE, ok = FALSE;
    UInt32 t0;

    g_hev.bits = 0;
    g_hev.connResult = 0;

    ep = OTOpenEndpoint(OTCreateConfiguration(kTCPName), 0, NULL, &err);
    if (ep == 0 || err != kOTNoError) return FALSE;

    OTMemzero(&b, sizeof(b));
    err = OTBind(ep, &b, NULL);
    if (err != kOTNoError) { OTCloseProvider(ep); return FALSE; }

    err = OTSetAsynchronous(ep);
    if (err != kOTNoError) { OTCloseProvider(ep); return FALSE; }
    err = OTInstallNotifier(ep, NewOTNotifyUPP(HttpNotifier), NULL);
    if (err != kOTNoError) { OTCloseProvider(ep); return FALSE; }

    OTMemzero(&call, sizeof(call));
    OTMemzero(&addr, sizeof(addr));
    OTInitInetAddress(&addr, (InetPort)port_to_nbo((UInt16)port),
                      (InetHost)ip_to_nbo(host));
    call.addr.buf = (UInt8 *)&addr;
    call.addr.len = call.addr.maxlen = (long)sizeof(addr);

    err = OTConnect(ep, &call, NULL);
    if (err != kOTNoError && err != (OSStatus)kOTNoDataErr) {
        char m[96];
        sprintf(m, "** update: OTConnect err=%ld", (long)err);
        push_line(m);
        OTCloseProvider(ep); return FALSE;
    }
    if (!http_wait_bits(T_CONNECT, 30u * 60u) || g_hev.connResult != noErr) {
        char m[96];
        sprintf(m, "** update: no T_CONNECT (res=%ld)",
                (long)g_hev.connResult);
        push_line(m);
        OTCloseProvider(ep); return FALSE;
    }

    /* On an async endpoint the connection isn't usable until we accept
     * the completion with OTRcvConnect (same as the chat path's
     * pump_ot_events; omitting it left the endpoint in T_OUTCON and every
     * OTSnd failed with zero bytes on the wire, 2026-09-28). */
    {
        OSStatus crc = OTRcvConnect(ep, NULL);
        if (crc != noErr) {
            char m[96];
            sprintf(m, "** update: rcvconnect=%ld", (long)crc);
            push_line(m);
            OTCloseProvider(ep); return FALSE;
        }
    }

    sprintf(req, "GET /%s HTTP/1.0\r\nHost: %s\r\n\r\n", path, host);
    {
        char *rp = req;
        long left = (long)strlen(req);
        while (left > 0) {
            long w = (long)OTSnd(ep, rp, left, 0);
            if (w > 0) { rp += w; left -= w; continue; }
            if (w == (long)kOTNoDataErr) {
                if (!http_wait_bits(T_GODATA, 30u * 60u)) {
                    OTCloseProvider(ep); return FALSE;
                }
                continue;
            }
            OTCloseProvider(ep); return FALSE;
        }
    }

    t0 = TickCount();
    while (1) {
        long n;
        OTFlags rflags;
        if ((TickCount() - t0) > 300u * 60u) break;   /* 5 min transfer cap */
        n = (long)OTRcv(ep, buf, sizeof(buf), &rflags);
        if (n == (long)kOTNoDataErr) {
            if (!http_wait_bits(T_DATA, 30u * 60u)) break;
            continue;
        }
        if (n <= 0) break;                            /* EOF or error */
        if (!headerDone) {
            long room = (long)sizeof(hdr) - 1 - hdrLen;
            long take = (n < room) ? n : room;
            long idx, rest;
            char *eoh;
            if (take > 0) { memcpy(hdr + hdrLen, buf, take); hdrLen += take; }
            hdr[hdrLen] = 0;
            eoh = strstr(hdr, "\r\n\r\n");
            if (eoh == NULL) {
                if (hdrLen >= (long)sizeof(hdr) - 1) break;  /* header overflow */
                continue;
            }
            *eoh = 0;
            {
                char *cl = strstr(hdr, "Content-Length:");
                if (cl == NULL) cl = strstr(hdr, "content-length:");
                if (cl) contentLen = atol(cl + 15);
            }
            headerDone = TRUE;
            idx = (long)(eoh - hdr) + 4;
            rest = hdrLen - idx;
            if (rest > 0) { FSWrite(forkFd, &rest, hdr + idx); total += rest; }
            if (contentLen >= 0 && total >= contentLen) { ok = TRUE; break; }
            continue;
        }
        FSWrite(forkFd, &n, buf);
        total += n;
        if (contentLen >= 0 && total >= contentLen) { ok = TRUE; break; }
        redraw();                                     /* keep UI alive/progress */
    }
    if (!ok && headerDone && contentLen < 0 && total > 0) ok = TRUE;
    OTCloseProvider(ep);
    return ok;
}

static Boolean http_get(const char *host, int port, const char *path, FSSpec *dest)
{
    SInt16 df = 0;
    Boolean ok;
    OSErr err;
    /* Granular diagnostics: each stage reports its OSErr on screen
     * (2026-09-28: a bare "download failed" hid which stage broke). */
    err = FSpCreate(dest, APP_CREATOR, 'APPL', 0 /* system script */);
    if (err != noErr && err != dupFNErr) {
        char m[96];
        sprintf(m, "** update: create err=%d", (int)err);
        push_line(m);
        return FALSE;
    }
    err = FSpOpenDF(dest, fsRdWrCmd, &df);
    if (err != noErr) {
        char m[96];
        sprintf(m, "** update: openDF err=%d", (int)err);
        push_line(m);
        return FALSE;
    }
    SetFPos(df, fsFromStart, 0);
    ok = http_fetch(host, port, path, df);
    FSClose(df);
    if (!ok) push_line("** update: fetch failed (net)");
    return ok;
}

/* fetch raw resource-fork payload into the destination file's RF */
static Boolean http_get_rsrc(const char *host, int port, const char *path,
                            FSSpec *dest)
{
    SInt16 rf = 0;
    Boolean ok;
    if (FSpOpenRF(dest, fsRdWrCmd, &rf) != noErr) return FALSE;
    SetFPos(rf, fsFromStart, 0);
    ok = http_fetch(host, port, path, rf);
    FSClose(rf);
    return ok;
}

static void do_update(const char *name)
{
    ProcessSerialNumber psn;
    ProcessInfoRec info;
    FSSpec me, dest;
    FSRef ref;
    FSCatalogInfo ci;
    Str255 nm;
    Str255 ownNm;
    Str255 prevNm;
    Boolean stashed = FALSE;
    long ownL;
    long n;

    if (!valid_address(gPrefs.host)) {
        set_status("connect to a relay before updating");
        return;
    }

    /* Restore us if the update can't complete after we stashed. */
    #define UNSTASH() do {                                        \
        if (stashed) {                                            \
            FSSpec ps;                                            \
            if (FSMakeFSSpec(me.vRefNum, ci.parentDirID,          \
                            prevNm, &ps) == noErr)                \
                FSpRename(&ps, ownNm);                            \
            stashed = FALSE;                                      \
        }                                                         \
    } while (0)

    if (gConnected) {
        close_conn();
        set_status("update: link closed");
    }

    GetCurrentProcess(&psn);
    OTMemzero(&info, sizeof(info));
    info.processInfoLength = sizeof(info);
    info.processName = NULL;
    info.processAppSpec = &me;
    if (GetProcessInformation(&psn, &info) != noErr) {
        fail_update("** update: cannot locate my own file");
        return;
    }
    if (FSpMakeFSRef(&me, &ref) != noErr) {
        fail_update("** update: FSRef failed");
        return;
    }
    if (FSGetCatalogInfo(&ref, kFSCatInfoParentDirID, &ci, NULL, NULL, false) != noErr) {
        fail_update("** update: catalog info failed");
        return;
    }
    n = (long)strlen(name);
    if (n > 255) n = 255;
    nm[0] = (unsigned char)n;
    memcpy(nm + 1, name, n);

    /* Replace-in-place (2026-09-29): stash OUR running copy under a
     * ".PREV" suffix and land the fresh build under OUR OWN name.  The
     * folder then holds exactly one live copy whose title carries the
     * version number; the next launch deletes the ".PREV".  This is the
     * fix for the stale ".NEW" pile that let a wrong build get launched.
     * If the stash fails we fall back to the old copy-alongside flow. */
    ownL = (long)me.name[0];
    if (ownL > 0 && ownL <= 248) {
        OSErr rerr;
        memcpy(ownNm, me.name, ownL + 1);
        prevNm[0] = (unsigned char)(ownL + 5);
        memcpy(prevNm + 1, ownNm + 1, ownL);
        memcpy(prevNm + 1 + ownL, ".PREV", 5);
        rerr = FSpRename(&me, prevNm);
        if (rerr == dupFNErr) {      /* stale PREV from an aborted cycle */
            FSSpec oldPrev;
            if (FSMakeFSSpec(me.vRefNum, ci.parentDirID, prevNm, &oldPrev) == noErr) {
                FSpDelete(&oldPrev);
                rerr = FSpRename(&me, prevNm);
            }
        }
        if (rerr == noErr) {
            if (FSMakeFSSpec(me.vRefNum, ci.parentDirID, ownNm, &dest) == fnfErr) {
                stashed = TRUE;
            } else {
                stashed = TRUE;      /* so UNSTASH can undo it */
                UNSTASH();           /* our name is taken — fall back */
            }
        }
    }
    if (!stashed) {
        /* The target file may not exist yet: FSMakeFSSpec reports fnfErr
         * (-43) while still filling in a usable spec. Only fail on OTHER
         * errors (2026-09-28: treating fnfErr as fatal broke self-update
         * in the field with "bad target spec"). */
        OSErr fserr = FSMakeFSSpec(me.vRefNum, ci.parentDirID, nm, &dest);
        if (fserr != noErr && fserr != fnfErr) {
            fail_update("** update: bad target spec");
            return;
        }
    }
    set_status("downloading update...");
    if (http_get(gPrefs.host, gPrefs.updatePort, name, &dest)) {
        {
            char rpath[128];
            sprintf(rpath, "%s.rsrc", name);
            if (http_get_rsrc(gPrefs.host, gPrefs.updatePort, rpath, &dest)) {
                push_line("** update resources installed");
            } else {
                push_line("** WARNING: resource fork fetch failed");
            }
        }
        push_line("** update downloaded, relaunching");
        redraw();
        Delay(20, NULL);
        {
            LaunchParamBlockRec pb;
            memset(&pb, 0, sizeof(pb));
            pb.launchBlockID = extendedBlock;
            pb.launchEPBLength = extendedBlockLen;
            pb.launchAppSpec = &dest;
            if (LaunchApplication(&pb) == noErr) {
                quit_app();
            }
        }
        fail_update("** update: launch failed");
        UNSTASH();       /* couldn't launch — put ourselves back */
    } else {
        UNSTASH();       /* bad download — put ourselves back */
        fail_update("** update: download failed");
    }
    #undef UNSTASH
}

/* ---------------- control ---------------- */
/* Verb match is case-insensitive: the operator console has historically sent
 * both "UPDATE " and "update " down the C: channel (2026-09-28: lowercase
 * update was silently ignored by an uppercase-only compare, breaking
 * self-update in the field). */
static Boolean verb_is(const char *cmd, const char *verb)
{
    while (*verb) {
        char a = *cmd++, b = *verb++;
        if (a >= 'a' && a <= 'z') a -= (char)('a' - 'A');
        if (b >= 'a' && b <= 'z') b -= (char)('a' - 'A');
        if (a != b) return FALSE;
    }
    return (*cmd == ' ' || *cmd == 0);
}

static void do_control(char *cmd)
{
    if (RemoteMacHandle(cmd)) return;
    if (verb_is(cmd, "quit")) {
        push_line("** quit by operator");
        quit_app();
    } else if (verb_is(cmd, "screen")) {
        push_line("** screenshot requested");
        do_screen();
    } else if (verb_is(cmd, "update")) {
        const char *arg = cmd + 6;
        while (*arg == ' ') arg++;
        do_update(arg);
    }
}

/* ---------------- line dispatch ---------------- */
static void begin_agent_message(void)
{
    if (!gNeedAgentTag) return;
    flush_cur();
    gPendKind = 2; gPendFirst = 1;
    gPendTint = (int)(gMsgSeq & 1); gMsgSeq++;
    now_hhmm(gPendTime);
    cur_append("Agent: ", sizeof("Agent: ") - 1);
    gNeedAgentTag = FALSE;
}

static void handle_line(char *line)
{
    char tag;
    char *payload;

    if (!*line) return;
    tag = line[0];
    if (line[1] != ':') return;
    payload = line + 2;

    switch (tag) {
        case 'H':
            if (!strcmp(payload, "alive")) gRelayHeartbeat = TRUE;
            break;
        case 'I':
            if (!strncmp(payload, "BEGIN\t", 6)) gBusy = TRUE;
            ToolQuestionsLine(payload); gDirty = TRUE;
            break;
        case 'G':
            if (!strncmp(payload, "ROW\t", 4)) {
                begin_agent_message();
                flush_cur();
                if (gPendFirst) { push_line_meta(gPendKind == 2 ? gPendName : "", gPendKind, 1, gPendTint, gPendTime); gPendFirst = 0; }
                push_image_row(payload + 4, gPendKind, gPendTint);
            } else ChatImagesLine(payload);
            break;
        case 'F': case 'Y':
            model_line(tag, payload);
            break;
        case 'W':
            ModelSettingsLine(payload);
            break;
        case 'B':
            flush_cur();
            strncpy(gPendName, payload, sizeof gPendName - 1);
            gNeedAgentTag = TRUE;
            break;
        case 'J': case 'V': case 'K': case 'Z':
            history_line(tag, payload);
            break;
        case 'T':
            begin_agent_message();
            unescape_append(payload, (long)strlen(payload));
            break;
        case 'S':
            if (!strcmp(payload, "thinking...") && !gBusy) {
                gBusy = TRUE; gDirty = gNavDirty = TRUE;
            }
            set_status(payload);
            if (strcmp(payload, "connected") == 0) {
                gRelayGreeted = TRUE;
                send_line("H:questions");
                send_line("F:");
                send_line("L:");
                if (gPrefs.lastID[0]) request_conversation(gPrefs.lastID);
                else if (gPrefs.model[0]) request_model(gPrefs.model);
            } else if (strcmp(payload, "reply stopped") == 0) push_line("** reply stopped");
            break;
        case 'D':
            flush_cur();
            ToolQuestionsReset();
            gBusy = FALSE;
            gDirty = gNavDirty = TRUE;
            set_status(gConnected ? "ready" : "offline");
            send_line("L:");
            break;
        case 'X':
            flush_cur();
            push_line(payload);
            gBusy = FALSE;
            gDirty = gNavDirty = TRUE;
            break;
        case 'C':
            do_control(payload);
            break;
        default:
            break;
    }
}

/* ---------------- prompt send ---------------- */
static Boolean draft_has_text(void)
{
    long i, n;
    Handle text;
    Boolean found = FALSE;
    if (!gTE) return FALSE;
    n = (*gTE)->teLength;
    text = (*gTE)->hText;
    HLock(text);
    for (i = 0; i < n; i++)
        if ((*text)[i] > ' ') { found = TRUE; break; }
    HUnlock(text);
    return found;
}

static void send_prompt(void)
{
    if (ToolQuestionsPending()) { ToolQuestionsOpen(); return; }
    char buf[DRAFT_LIMIT + 3];
    long len;
    Handle text;
    if (gBusy || gHistoryLoading || ChatImagesUploading() || !gConnected || !gTE || (!draft_has_text() && !gDraftImage[0])) return;
    if (gDraftImage[0] && (!gModelSupportsImages || !ChatImagesReady(gDraftImage))) {
        set_status(gModelSupportsImages ? "Picture is not ready" : "This contact does not accept pictures"); return;
    }
    len = (*gTE)->teLength;
    if (len > DRAFT_LIMIT || gQLen + 2 * (len + 2) + 1 > (long)sizeof(gQ)) {
        set_status("Message could not be queued; draft retained"); SysBeep(3);
        return;
    }
    text = (*gTE)->hText;
    HLock(text);
    BlockMoveData(*text, buf + 2, len);
    HUnlock(text);
    buf[0] = 'P'; buf[1] = ':'; buf[len + 2] = 0;
    if (gDraftImage[0]) { char image[80]; sprintf(image, "Q:%s", gDraftImage); send_line(image); }
    send_line(buf);
    gPendKind = 1; gPendFirst = 1;
    gPendTint = (int)(gMsgSeq++ & 1);
    now_hhmm(gPendTime);
    cur_append("You: ", 5);
    cur_append(buf + 2, len);
    flush_cur();
    if (gDraftImage[0]) {
        if (gPendFirst) { push_line_meta("", 1, 1, gPendTint, gPendTime); gPendFirst = 0; }
        push_image_row(gDraftImage, 1, gPendTint);
        gDraftImage[0] = 0; layout_chat();
    }
    gNeedAgentTag = TRUE;
    TESetText("", 0, gTE);
    TESetSelect(0, 0, gTE);
    cache_draft();
    gBusy = TRUE;
    gDirty = gNavDirty = TRUE;
    set_status("thinking...");
}

/* ---------------- native color QuickDraw drawing ---------------- */
/* Map a 0..255 sRGB triple onto a Classic RGBColor (0..65535). */
static RGBColor rgb255(int r, int g, int b)
{
    RGBColor c;
    c.red   = (unsigned short)(r * 257);
    c.green = (unsigned short)(g * 257);
    c.blue  = (unsigned short)(b * 257);
    return c;
}
/* Native RGB colors require a color graphics port (NewCWindow). A 32-bit
 * screen alone does not make a NewWindow monochrome port color-capable. */
#define C_BG        rgb255(255,255,255)
#define C_BODY      rgb255(17,17,17)
#define C_TEXT      rgb255(17,17,17)
#define C_TEXT2     rgb255(102,102,102)
#define C_SYS       rgb255(102,102,102)
#define C_TS        rgb255(102,102,102)
#define C_AV_YOU    rgb255(45,125,210)
#define C_AV_AGENT  rgb255(90,103,210)
#define C_AV_INIT   rgb255(255,255,255)
#define C_TOOL_BG   rgb255(238,238,238)
#define C_TOOL_SEP  rgb255(160,160,160)
#define C_TOOL_INK  rgb255(158,158,158)  /* unsupported formatting is dimmed */
#define C_BTN_EDGE  rgb255(85,85,85)
#define C_OK        rgb255(46,182,125)
#define C_BAD       rgb255(214,72,60)
#define C_SURROUND  rgb255(221,221,221)
#define C_BAND1     rgb255(255,255,255)
#define C_BAND2     rgb255(238,238,238)
#define C_LAV_HDR   rgb255(215,212,234)
#define C_ACCENT    rgb255(102,102,204)
static void setfg(RGBColor c) { RGBForeColor(&c); }
static void setbg(RGBColor c) { RGBBackColor(&c); }
static void fillc(Rect *r, RGBColor c) { setbg(c); EraseRect(r); }
static void setband(int isB) { setbg(isB ? C_BAND2 : C_BAND1); }
static void fillband(Rect *r, int isB) { setband(isB); EraseRect(r); }

/* Resolve REAL font metrics once (plan §5).  Font point size is NOT a safe
 * substitute for line height, so we ask the font for ascent/descent/leading
 * via GetFontInfo and derive the row advances from that. */
static void init_metrics(void)
{
    FontInfo fi;

    /* Body: Geneva 12 proportional (plan: conversation stays proportional). */
    TextFont(kFontIDGeneva);
    TextSize(12);
    TextFace(0);
    GetFontInfo(&fi);
    if (fi.ascent <= 0) fi.ascent = 11;          /* defensive fallback */
    gBodyAsc = fi.ascent;
    gBodyLineH = fi.ascent + fi.descent + fi.leading + 4;

    /* System notices: Geneva 11. */
    TextSize(11);
    GetFontInfo(&fi);
    if (fi.ascent <= 0) fi.ascent = 10;
    gSysLineH = fi.ascent + fi.descent + fi.leading + 3;

    /* Resolve Chicago by name; absence of a compile-time constant says
     * nothing about whether the font is installed. */
    {
        Str255 name;
        GetFNum("\pChicago", &gHeadingFont);
        GetFontName(gHeadingFont, name);
        if (name[0] == 7 && memcmp(name + 1, "Chicago", 7) == 0)
            gHeadingFace = 0;
        else { gHeadingFont = kFontIDGeneva; gHeadingFace = bold; }
    }
    TextFont(gHeadingFont); TextSize(12); TextFace(gHeadingFace);
    GetFontInfo(&fi);
    gNameAsc = fi.ascent;
    gHdrH = MSG_PAD_Y + fi.ascent + fi.descent + fi.leading + 2;

    /* Restore default text state. */
    TextFont(kFontIDGeneva);
    TextSize(12);
    TextFace(0);
}

static void draw_body(short x, short y, const char *s)
{
    TextFont(kFontIDGeneva); TextSize(12); TextFace(0);
    setfg(C_TEXT); MoveTo(x, y);
    DrawText(s, 0, (short)strlen(s));
}

/* A single centred letter in a tool button (face = QuickDraw style bits). */
static void glyph_letter(short x, char ch, int face)
{
    Str255 s;
    long w;
    s[0] = 1; s[1] = (unsigned char)ch;
    TextFont(kFontIDGeneva);
    TextSize(12);
    TextFace(face);
    setfg(C_TOOL_INK);
    setbg(C_TOOL_BG);
    w = StringWidth(s);
    MoveTo((short)(x + (22 - w + 1) / 2), (short)((TOOL_T + TOOL_B) / 2 + 4));
    DrawString(s);
}

/* Draw a monospace label centred in a tool button (used for "</>"). */
static void glyph_mono(short x, const char *txt)
{
    Str255 s;
    long w, n = (long)strlen(txt);
    if (n > 255) n = 255;
    s[0] = (unsigned char)n;
    memcpy(s + 1, txt, (size_t)n);
    TextFont(kFontIDMonaco);
    TextSize(9);
    TextFace(0);
    setfg(C_TOOL_INK);
    setbg(C_TOOL_BG);
    w = StringWidth(s);
    MoveTo((short)(x + (22 - w + 1) / 2), (short)((TOOL_T + TOOL_B) / 2 + 3));
    DrawString(s);
}

static void glyph_list(short x)
{
    short cy = (short)((TOOL_T + TOOL_B) / 2);
    int k;
    setfg(C_TOOL_INK);
    setbg(C_TOOL_BG);
    for (k = 0; k < 3; k++) {
        short ly = (short)(cy - 5 + k * 5);
        Rect d = { ly, (short)(x + 4), (short)(ly + 2), (short)(x + 6) };
        PaintRect(&d);
        MoveTo((short)(x + 9), (short)(ly + 1));
        LineTo((short)(x + 18), (short)(ly + 1));
    }
}

static void glyph_link(short x)
{
    short cy = (short)((TOOL_T + TOOL_B) / 2);
    Rect o1 = { (short)(cy - 2), (short)(x + 4),  (short)(cy + 2), (short)(x + 10) };
    Rect o2 = { (short)(cy - 2), (short)(x + 12), (short)(cy + 2), (short)(x + 18) };
    setfg(C_TOOL_INK);
    setbg(C_TOOL_BG);
    FrameOval(&o1);
    FrameOval(&o2);
}

static void glyph_smile(short x)
{
    short cy = (short)((TOOL_T + TOOL_B) / 2);
    Rect f = { (short)(cy - 6), (short)(x + 5), (short)(cy + 6), (short)(x + 17) };
    setfg(C_TOOL_INK);
    setbg(C_TOOL_BG);
    FrameOval(&f);
    {
        Rect e1 = { (short)(cy - 3), (short)(x + 8),  (short)(cy - 1), (short)(x + 10) };
        Rect e2 = { (short)(cy - 3), (short)(x + 12), (short)(cy - 1), (short)(x + 14) };
        PaintRect(&e1);
        PaintRect(&e2);
    }
    FrameArc(&f, 35, 110);      /* lower arc -> a smile */
}

/* Plain-text transport does not support formatting. Show the reference
 * toolbar in a dimmed, noninteractive state rather than promising actions. */
static void draw_toolbar(void)
{
    Rect band = { TOOL_T, IN_X, TOOL_B, IN_R };
    RGBColor bg = C_TOOL_BG;
    short uy = (short)((TOOL_T + TOOL_B) / 2 + 7);
    short sy = (short)((TOOL_T + TOOL_B) / 2);
    PenNormal();
    PenMode(srcCopy);
    TextMode(srcCopy);
    fillc(&band, bg);
    setfg(C_BTN_EDGE); FrameRect(&band);

    glyph_letter(14, 'B', bold);
    glyph_letter(38, 'I', italic);
    glyph_letter(62, 'U', 0);
    setfg(C_TOOL_INK); PenMode(srcCopy);
    MoveTo(68, uy); LineTo(78, uy);          /* underline under the U */
    glyph_letter(86, 'S', 0);
    setfg(C_TOOL_INK); PenMode(srcCopy);
    MoveTo(92, sy); LineTo(102, sy);         /* strike through the S */

    PenMode(srcCopy); MoveTo(116, (short)(TOOL_T + 4)); LineTo(116, (short)(TOOL_B - 4));
    glyph_mono(122, "</>");
    glyph_list(146);

    PenMode(srcCopy); MoveTo(176, (short)(TOOL_T + 4)); LineTo(176, (short)(TOOL_B - 4));
    glyph_link(182);
    glyph_smile(206);
}

/* Framed 32x32 avatar with crisp QuickDraw artwork (plan §4: NO letter tiles).
 * "You" = a person (head + shoulders); "Agent" = a computer/monitor.
 * Clip to the box so the glyph never bleeds into the text column. */
static void draw_avatar(int kind, short bx, short by)
{
    Rect box = { by, bx, (short)(by + AV_SZ), (short)(bx + AV_SZ) };
    RgnHandle keep = NewRgn();
    short cx = (short)(bx + AV_SZ / 2);
    RGBColor fill = (kind == 1) ? C_AV_YOU : C_AV_AGENT;

    fillc(&box, fill);
    GetClip(keep);
    ClipRect(&box);
    setfg(C_AV_INIT);
    if (kind == 1) {
        /* head */
        Rect head = { (short)(by + 8), (short)(cx - 4),
                     (short)(by + 16), (short)(cx + 4) };
        PaintOval(&head);
        /* shoulders: a wide oval whose top arc forms a rounded mound */
        Rect sh = { (short)(by + 19), (short)(cx - 10),
                    (short)(by + 42), (short)(cx + 10) };
        PaintOval(&sh);
    } else {
        /* monitor body, punched-out screen, stand + base */
        Rect mon = { (short)(by + 8), (short)(bx + 5),
                    (short)(by + 22), (short)(bx + AV_SZ - 5) };
        Rect scr = { (short)(by + 10), (short)(bx + 7),
                    (short)(by + 20), (short)(bx + AV_SZ - 7) };
        Rect stand = { (short)(by + 22), (short)(cx - 2),
                      (short)(by + 25), (short)(cx + 2) };
        Rect base  = { (short)(by + 25), (short)(bx + 9),
                      (short)(by + 27), (short)(bx + AV_SZ - 9) };
        PaintRect(&mon);
        fillc(&scr, fill);          /* punch the screen back to the chip hue */
        setfg(C_AV_INIT);
        PaintRect(&stand);
        PaintRect(&base);
    }
    SetClip(keep);
    DisposeRgn(keep);
    setfg(C_BTN_EDGE);
    PenSize(1, 1);
    FrameRect(&box);
}

/* One transcript row.  Band colour is supplied by the caller so the band fill
 * (done outside) and the text background always agree (no white halos).
 *  kind==0        -> system notice, full width, muted
 *  first!=0       -> message header (avatar + name + timestamp)
 *  else           -> one wrapped body line at the body column            */
static void draw_row(int kind, int first, const char *tstamp, const char *s,
                    short msgLeft, short lineTop, short lineH, int isB)
{
    Str255 p;
    short textcol = (short)(gTxRect.left + MSG_PAD_X + BODY_INDENT);
    long n = (long)strlen(s);

    if (kind == 0) {
        const char *t = s;
        if (n >= 3 && t[0] == '*' && t[1] == '*' && t[2] == ' ') { t += 3; n -= 3; }
        if (n > 255) n = 255;
        TextFont(kFontIDGeneva);
        TextSize(11);
        TextFace(0);
        setfg(C_SYS);
        setband(isB);
        p[0] = (unsigned char)n;
        memcpy(p + 1, t, n);
        MoveTo((short)(gTxRect.left + MSG_PAD_X), (short)(lineTop + 12));
        DrawString(p);
        return;
    }

    if (first) {
        short boxTop = (short)(lineTop + MSG_PAD_Y);
        const char *nm = (s && s[0]) ? s : ((kind == 1) ? "You" : "Agent");
        long ml = (long)strlen(nm);
        short base = (short)(boxTop + gNameAsc);
        long nw;
        draw_avatar(kind, (short)(msgLeft + MSG_PAD_X), boxTop);
        TextFont(gHeadingFont);
        TextSize(12);
        TextFace(gHeadingFace);
        setfg(C_TEXT);
        setband(isB);
        if (ml > 255) ml = 255;
        p[0] = (unsigned char)ml;
        memcpy(p + 1, nm, ml);
        MoveTo(textcol, base);
        DrawString(p);
        nw = StringWidth(p);
        if (tstamp && tstamp[0]) {
            long tl = (long)strlen(tstamp);
            if (tl > 255) tl = 255;
            TextSize(9);
            TextFace(0);
            setfg(C_TS);
            p[0] = (unsigned char)tl;
            memcpy(p + 1, tstamp, tl);
            if (nw + 42 > gTxRect.right - MSG_PAD_X - textcol)
                MoveTo(textcol, (short)(base + 12));
            else MoveTo((short)(textcol + nw + 6), base);
            DrawString(p);
        }
        return;
    }

    /* body line */
    if (n == 0) return;                 /* blank line: band already painted */
    if (n > 255) n = 255;
    setband(isB);
    draw_body(textcol, (short)(lineTop + gBodyAsc + 2), s);
}

/* Variable-height transcript selection tracks through the normal event loop.
 * QuickDraw measures the real fonts; its native highlight honors OS settings.
 * TextEdit continues to own the editable composer. */
static const char *selection_row(long row, short *column, short *height) {
    long idx = row % MAXLINES; unsigned char meta = gLineMeta[idx];
    const char *body = copy_row_text(idx);
    *column = gTxRect.left + MSG_PAD_X + BODY_INDENT; *height = gBodyLineH;
    TextFont(kFontIDGeneva); TextSize(12); TextFace(0);
    if (!(meta & 3)) {
        *column = gTxRect.left + MSG_PAD_X; *height = gSysLineH;
        TextSize(11); if (!strncmp(body, "** ", 3)) body += 3;
    } else if (meta & 4) {
        TextFont(gHeadingFont); TextFace(gHeadingFace); *height = gNameAsc + 4;
    }
    return body;
}
static TranscriptPosition text_hit(Point p) {
    TranscriptPosition pos = {-1,0};
    long base = gLineCount > MAXLINES ? gLineCount - MAXLINES : 0, top = 0;
    long viewTop = gDocHeight - gScroll - tx_viewh(), y;
    if (viewTop < 0) viewTop = 0;
    y = p.v - gTxRect.top + viewTop;
    if (!gLineCount) return pos;
    for (pos.row = base; pos.row < gLineCount - 1; pos.row++) {
        if (y < top + gLineH[pos.row % MAXLINES]) break;
        top += gLineH[pos.row % MAXLINES];
    }
    {
        short column, height, len, x; const char *body = selection_row(pos.row, &column, &height);
        len = strlen(body); x = p.h - column;
        if (y < 0) return (TranscriptPosition){base,0};
        if (y >= top + gLineH[pos.row % MAXLINES]) return (TranscriptPosition){pos.row,len};
        while (pos.offset < len && x > TextWidth(body, 0, pos.offset) + CharWidth(body[pos.offset]) / 2) pos.offset++;
    }
    return pos;
}
static void text_highlight(long row, short top) {
    TranscriptPosition a, b; short column, height, start, end, dy = 0; const char *body; Rect r;
    if (!has_text_selection()) return;
    text_range(&a, &b); if (row < a.row || row > b.row) return;
    body = selection_row(row, &column, &height);
    start = row == a.row ? a.offset : 0; end = row == b.row ? b.offset : strlen(body);
    if (start > strlen(body)) start = strlen(body); if (end > strlen(body)) end = strlen(body);
    if (end <= start) return;
    if (gLineMeta[row % MAXLINES] & 4) dy = MSG_PAD_Y;
    SetRect(&r, column + TextWidth(body, 0, start), top + dy,
            column + TextWidth(body, 0, end), top + dy + height);
    LMSetHiliteMode(LMGetHiliteMode() & ~0x80); InvertRect(&r);
}
static void text_drag(Point point) {
    TranscriptPosition next;
    SetPort(gWin);
    if (point.v < gTxRect.top) set_scroll(gScroll + 12);
    else if (point.v >= gTxRect.bottom) set_scroll(gScroll - 12);
    next = text_hit(point);
    if (next.row != gTextFocus.row || next.offset != gTextFocus.offset) { gTextFocus = next; gDirty = TRUE; }
}
static void select_transcript(void) {
    short col, height;
    if (!gLineCount) return;
    gSelectedMessage = -1; gTextAnchor.row = gLineCount > MAXLINES ? gLineCount - MAXLINES : 0;
    gTextAnchor.offset = 0; gTextFocus.row = gLineCount - 1;
    gTextFocus.offset = strlen(selection_row(gTextFocus.row, &col, &height)); gDirty = TRUE;
}
static void copy_text_selection(void) {
    TranscriptPosition a, b; long row, bytes = 0; Ptr text;
    text_range(&a, &b); text = NewPtr((b.row - a.row + 1) * (LINEBUF + 1));
    if (!text) { set_status("not enough memory to copy"); return; }
    for (row = a.row; row <= b.row; row++) {
        short col, height, start, end; const char *body = selection_row(row, &col, &height);
        start = row == a.row ? a.offset : 0; end = row == b.row ? b.offset : strlen(body);
        if (start > strlen(body)) start = strlen(body); if (end > strlen(body)) end = strlen(body);
        if (end > start) { memcpy(text + bytes, body + start, end - start); bytes += end - start; }
        if (row < b.row) {
            if ((gLineMeta[row % MAXLINES] & 4) || gLineBreak[row % MAXLINES] == 1 || gLineBreak[row % MAXLINES] == 3) text[bytes++] = '\r';
            else if (!gLineBreak[row % MAXLINES]) text[bytes++] = ' ';
        }
    }
    ZeroScrap();
    if (PutScrap(bytes, 'TEXT', text) == noErr) { gSystemScrapOwned = TRUE; set_status("selection copied"); }
    else set_status("could not copy selection");
    DisposePtr(text);
}

/* Live-pending capture: collect the streamed body into a temp array so it can
 * be laid out as a full (header + body) message before it is flushed. */
static char pendBuf[2048][LINEBUF];
static long pendN = 0;
static void pend_emit(const char *seg, long len)
{
    if (pendN >= 2048) return;
    if (len > LINEBUF - 1) len = LINEBUF - 1;
    memcpy(pendBuf[pendN], seg, (size_t)len);
    pendBuf[pendN][len] = 0;
    pendN++;
}

static void paint_transcript(void)
{
    static long tops[MAXLINES];
    long eff, base, docH = 0, j;
    long viewH, bottomPx, topPx;
    RgnHandle old = NewRgn();

    GetClip(old);
    ClipRect(&gTxRect);
    TextMode(srcCopy);

    eff = gLineCount;
    if (eff > MAXLINES) eff = MAXLINES;
    base = gLineCount - eff;
    for (j = 0; j < eff; j++) {
        long idx = (base + j) % MAXLINES;
        tops[j] = docH;
        docH += gLineH[idx];
    }

    /* Pending (streaming) message contributes its height so scroll math and
     * auto-follow stay correct while a reply grows. */
    pendN = 0;
    long pendTop = docH, pendH = 0;
    int pk = 0, ptint = 0;
    if (gCurLen > 0) {
        const char *body = gCur;
        gCur[gCurLen] = 0;
        pk = gPendKind ? gPendKind : 0;
        ptint = gPendTint & 1;
        if (pk != 0) {
            long avail = (long)(gTxRect.right - gTxRect.left) - 2 * MSG_PAD_X - BODY_INDENT;
            if (strncmp(body, "You: ", 5) == 0) body += 5;
            else if (strncmp(body, "Agent: ", 7) == 0) body += 7;
            wrap_emit_lines(body, avail, pend_emit);
            pendH = gHdrH + pendN * (long)gBodyLineH + MSG_PAD_Y;
        } else {
            pendH = gSysLineH;
        }
    }

    if (!gFollow) gScroll += docH + pendH - gDocHeight;
    gDocHeight = docH + pendH;
    if (gFollow) gScroll = 0;   /* follow == pinned to the newest (bottom)    */
    if (gScroll < 0) gScroll = 0;
    {
        long mx = scroll_max();
        if (gScroll > mx) gScroll = mx;
    }
    viewH = tx_viewh();
    bottomPx = gDocHeight - gScroll;
    topPx = bottomPx - viewH;
    if (topPx < 0) { topPx = 0; bottomPx = viewH; }

    fillc(&gTxRect, C_BAND1);   /* paper: band A is white; band B paints over it */


    for (int pass = 0; pass < 2; pass++)
    for (j = 0; j < eff; j++) {
        long idx = (base + j) % MAXLINES;
        long T = tops[j], h = gLineH[idx];
        unsigned char md;
        int kind, first, tint, isB;
        Rect lr;
        if (T + h + AV_SZ <= topPx) continue;
        if (T >= bottomPx) break;
        md = gLineMeta[idx];
        kind = md & 3; first = (md >> 2) & 1; tint = (md >> 3) & 1;
        isB = (kind != 0 && (tint & 1));
        lr.left = gTxRect.left; lr.right = gTxRect.right;
        lr.top = (short)(gTxRect.top + (T - topPx));
        lr.bottom = (short)(lr.top + h);
        if (pass == 0) { fillband(&lr, isB); continue; }
        if (md & 16) {
            Rect image = { lr.top + MSG_PAD_Y, gTxRect.left + MSG_PAD_X + BODY_INDENT,
                           lr.bottom - MSG_PAD_Y, gTxRect.right - MSG_PAD_X };
            ChatImagesDraw(gLines[idx], &image);
        } else draw_row(kind, first, gLineTime[idx], gLines[idx],
                       (short)gTxRect.left, lr.top, (short)h, isB);
    }

    if (gCurLen > 0) {
        int isBp = (pk != 0 && (ptint & 1));
        long sy = gTxRect.top + (pendTop - topPx);
        if (pk == 0) {
            Rect r = { (short)sy, gTxRect.left, (short)(sy + gSysLineH), gTxRect.right };
            fillband(&r, isBp);
            draw_row(0, 1, "", gCur, (short)gTxRect.left, (short)sy,
                     (short)gSysLineH, isBp);
        } else {
            long s;
            Rect r = { (short)sy, gTxRect.left, (short)(sy + gHdrH), gTxRect.right };
            r.top = (short)(sy < gTxRect.top ? gTxRect.top : sy);
            r.bottom = (short)(sy + pendH > gTxRect.bottom ? gTxRect.bottom : sy + pendH);
            if (r.bottom > r.top) fillband(&r, isBp);
            if (sy + gHdrH + AV_SZ > gTxRect.top && sy < gTxRect.bottom)
                draw_row(pk, 1, gPendTime, pk == 2 ? gPendName : "", (short)gTxRect.left, (short)sy,
                         (short)gHdrH, isBp);
            sy += gHdrH;
            for (s = 0; s < pendN; s++) {
                long by = sy + s * (long)gBodyLineH;
                if (by + gBodyLineH <= gTxRect.top || by >= gTxRect.bottom) continue;
                draw_row(pk, 0, "", pendBuf[s], (short)gTxRect.left, (short)by,
                         (short)gBodyLineH, isBp);
            }
        }
    }

    if (!has_text_selection() && gSelectedMessage >= base && gSelectedMessage < gLineCount) {
        long first = gSelectedMessage - base, end = first + 1;
        long y1 = gTxRect.top + tops[first] - topPx, y2;
        Rect selection;
        while (end < eff && !(gLineMeta[(base + end) % MAXLINES] & 4)) end++;
        y2 = gTxRect.top + (end < eff ? tops[end] : docH) - topPx;
        if (y2 > gTxRect.top && y1 < gTxRect.bottom) {
            SetRect(&selection, gTxRect.left, y1 < gTxRect.top ? gTxRect.top : y1,
                    gTxRect.right, y2 > gTxRect.bottom ? gTxRect.bottom : y2);
            setfg(C_ACCENT); FrameRect(&selection);
        }
    }
    for (j = 0; j < eff; j++) {
        long y = gTxRect.top + tops[j] - topPx;
        if (y < gTxRect.bottom && y + gLineH[(base + j) % MAXLINES] > gTxRect.top) text_highlight(base + j, y);
    }
    /* restore a plain paper state for components drawn after us. */
    setbg(C_BG);
    setfg(C_BODY);
    SetClip(old);
    DisposeRgn(old);
}

/* System 7+ offscreen QuickDraw: assemble the bands, avatars and text before
 * copying one finished frame to the window. Never expose the erase pass. */
static void draw_transcript(void)
{
    Rect bounds = {0, 0, WIN_H, WIN_W};
    CGrafPtr saved;
    GDHandle device;
    PixMapHandle pixels;
    RGBColor fore, back;
    GetGWorld(&saved, &device);
    if (gTranscriptWorld && !EqualRect(&gTranscriptWorld->portRect, &bounds)) {
        DisposeGWorld(gTranscriptWorld); gTranscriptWorld = NULL;
    }
    if (!gTranscriptWorld) NewGWorld(&gTranscriptWorld, 32, &bounds, NULL, NULL, 0);
    pixels = gTranscriptWorld ? GetGWorldPixMap(gTranscriptWorld) : NULL;
    if (!pixels || !LockPixels(pixels)) {
        paint_transcript(); gTranscriptDirty = FALSE; return;
    }
    SetGWorld(gTranscriptWorld, NULL); PenNormal();
    paint_transcript();
    SetGWorld(saved, device);
    GetForeColor(&fore); GetBackColor(&back);
    ForeColor(blackColor); BackColor(whiteColor);
    CopyBits((BitMap *)*pixels, (BitMap *)*saved->portPixMap,
             &gTxRect, &gTxRect, srcCopy, NULL);
    RGBForeColor(&fore); RGBBackColor(&back);
    UnlockPixels(pixels);
    gTranscriptFrames++; gTranscriptDirty = FALSE;
}

/* Minimal footer: one connection dot + a short human word.  The technical
 * endpoint/model string that used to live here (and in the title) is gone. */
static void draw_status_strip(void)
{
    Rect r = { (short)(ST_Y - 2), TX_X, (short)WIN_H, (short)(WIN_W - 8) };
    Rect dot = { (short)(ST_Y + 1), (short)(TX_X + 2),
                (short)(ST_Y + 9), (short)(TX_X + 10) };
    Str255 p;
    long n;
    const char *w;
    char buf[128], animated[104];
    fillc(&r, C_SURROUND);
    setfg(gConnected ? C_OK : C_BAD);
    PaintOval(&dot);
    if (gConnected) {
        if (gStatus[0] && strcmp(gStatus, "ready") != 0) w = gStatus;
        else w = "connected";
    } else {
        w = "offline";
    }
    if (gConnected && gBusy) {
        long end;
        gThinkingFrame = (short)((TickCount() / 20) % 3);
        snprintf(animated, sizeof animated, "%.96s", w);
        end = strlen(animated);
        while (end && animated[end - 1] == '.') animated[--end] = 0;
        snprintf(animated + end, sizeof animated - end, "%.*s", gThinkingFrame + 1, "...");
        w = animated;
    } else gThinkingFrame = -1;
#ifdef AGENTCHAT_DEV_FIXTURE
    snprintf(buf, sizeof buf, "%s  VMTEST  v%s", w, AGENTCHAT_VERSION);
#else
    snprintf(buf, sizeof buf, "%s  v%s", w, AGENTCHAT_VERSION);
#endif
    w = buf;
    n = (long)strlen(w);
    if (n > 255) n = 255;
    p[0] = (unsigned char)n;
    memcpy(p + 1, w, n);
    TextFont(kFontIDGeneva);
    TextSize(9);
    TextFace(0);
    setfg(C_SYS);
    setbg(C_SURROUND);
    MoveTo((short)(TX_X + 15), (short)(ST_Y + 8));
    DrawString(p);
}

static void animate_status(void)
{
#ifndef AGENTCHAT_IDLE_BASELINE
    if (gWin && EmptyRgn(gWin->visRgn)) return;
#endif
    GrafPtr saved;
    if (!gWin || !gConnected || !gBusy || gThinkingFrame == (TickCount() / 20) % 3) return;
    /* Paint only the footer. The window's visibility region clips occluding
     * windows, so animation never erases or flashes the transcript. */
    GetPort(&saved); SetPort(gWin); draw_status_strip(); SetPort(saved);
}

/* Native composer (v29).  The field box (white paper + 1px frame) is ours; the
 * TEXT, the insertion caret and the selection inside it are owned by the
 * TextEdit Manager — TEUpdate redraws them from the record, TEIdle (driven from
 * the idle loop) blinks the caret.  The "Type your message" placeholder is
 * overlaid only while the record is empty, exactly like a real empty field
 * whose caret blinks on top of the ghost prompt.  No self-drawn caret/width
 * bookkeeping remains. */
static void draw_input_field(void)
{
    RgnHandle oldClip = NewRgn();
    PenNormal();
    PenMode(srcCopy);
    TextMode(srcCopy);
    fillc(&gInRect, C_BG);
    PenSize(1, 1); setfg(C_BTN_EDGE); FrameRect(&gInRect);

    GetClip(oldClip);
    if (gTE != NULL) {
        Rect er = gInRect;
        SetPort(gWin);
        InsetRect(&er, 1, 1);
        ClipRect(&er);
        TEUpdate(&gInRect, gTE);   /* redraw the record's text + selection */
    }
    if (gTE != NULL && (*gTE)->teLength == 0) {
        const char *ph = "Type your message";
        Str255    p;
        long      pn = (long)strlen(ph);
        p[0] = (unsigned char)pn;
        memcpy(p + 1, ph, (size_t)pn);
        TextFont(kFontIDGeneva);
        TextSize(12);
        TextFace(0);
        setfg(C_SYS);
        setbg(C_BG);
        MoveTo((short)((*gTE)->viewRect.left), (short)(gInRect.top + 16));
        DrawString(p);
    }
    if (gDraftImage[0]) {
        Rect image = {gInRect.top + 4, gInRect.left + 5, gInRect.top + 43, gInRect.left + 88};
        ChatImagesDraw(gDraftImage, &image);
    }
    SetClip(oldClip);
    DisposeRgn(oldClip);
}

/* DrawGrowIcon also draws full-width scrollbar gutter rules; clip it to
 * the actual native grow box because our scrollbars live in inset panels. */
static void draw_grow_icon(WindowPtr win)
{
    Rect corner = win->portRect;
    RgnHandle saved = NewRgn();
    GetClip(saved);
    corner.left = corner.right - 15; corner.top = corner.bottom - 15;
    ClipRect(&corner); DrawGrowIcon(win);
    SetClip(saved); DisposeRgn(saved);
}

/* Window Manager content regions are global. Keep the title and grow box
 * accessible even after dragging a window beyond the desktop. */
static Rect window_content_bounds(WindowPtr win)
{
    GrafPtr saved;
    Rect bounds = win->portRect;
    Point origin;
    origin.h = bounds.left; origin.v = bounds.top;
    GetPort(&saved); SetPort(win); LocalToGlobal(&origin); SetPort(saved);
    OffsetRect(&bounds, origin.h - bounds.left, origin.v - bounds.top);
    return bounds;
}

static void keep_window_visible(WindowPtr win)
{
    Rect screen = qd.screenBits.bounds;
    /* Hidden windows have empty content regions. Their graphics ports retain
     * the real geometry, so recovery works before ShowWindow as well. */
    Rect content = window_content_bounds(win);
    Rect vis = content;
    /* A collapsed (windowshade) window shows only its title bar, but its
     * port still describes the full uncollapsed size. Clamping the full
     * height would pin the shade near the top of the screen, so clamp
     * what the user actually sees: the structure region's bounds. */
    if (IsWindowCollapsed(win)) {
        RgnHandle struc = ((WindowPeek)win)->strucRgn;
        if (struc != NULL && *struc != NULL) {
            Rect sb = (**struc).rgnBBox;
            if (sb.right > sb.left && sb.bottom > sb.top) vis = sb;
        }
    }
    short left = vis.left, top = vis.top;
    short width = vis.right - vis.left, height = vis.bottom - vis.top;
    short minTop = screen.top + GetMBarHeight() + 24;
    if (left + width > screen.right - 4) left = screen.right - 4 - width;
    if (top + height > screen.bottom - 4) top = screen.bottom - 4 - height;
    if (left < screen.left + 4) left = screen.left + 4;
    if (top < minTop) top = minTop;
    if (left != vis.left || top != vis.top) {
        /* Move by the same delta so the port and structure stay aligned. */
        MoveWindow(win, content.left + (left - vis.left),
                   content.top + (top - vis.top), FALSE);
    }
}

static long grow_in_place(WindowPtr win, Point where, Rect *limits)
{
    Rect bounds = window_content_bounds(win), available = *limits;
    short maxW = qd.screenBits.bounds.right - 4 - bounds.left;
    short maxH = qd.screenBits.bounds.bottom - 4 - bounds.top;
    long size;
    if (maxW < available.left) maxW = available.left;
    if (maxH < available.top) maxH = available.top;
    if (available.right > maxW) available.right = maxW;
    if (available.bottom > maxH) available.bottom = maxH;
    size = GrowWindow(win, where, &available);
    if (size) {
        SizeWindow(win, (short)(size & 0xffff), (short)((unsigned long)size >> 16), TRUE);
        /* Growing changes size, never the content's top-left anchor. */
        MoveWindow(win, bounds.left, bounds.top, FALSE);
        keep_window_visible(win);
    }
    return size;
}

static void arrange_windows(void)
{
    long oldh = tx_viewh();
    CollapseWindow(gNavWin, FALSE); CollapseWindow(gWin, FALSE);
    gNavW = 210; gNavH = 500; gChatW = 550; gChatH = 500;
    SizeWindow(gNavWin, gNavW, gNavH, TRUE);
    SizeWindow(gWin, gChatW, gChatH, TRUE);
    MoveWindow(gNavWin, 14, 60, FALSE); MoveWindow(gWin, 236, 60, FALSE);
    keep_window_visible(gNavWin); keep_window_visible(gWin);
    ShowWindow(gNavWin); ShowWindow(gWin); gNavVisible = TRUE;
    SetPort(gNavWin); layout_nav();
    SetPort(gWin); layout_chat(); reflow_transcript(oldh);
    SelectWindow(gWin); gDirty = gNavDirty = TRUE;
}

/* Navigation uses Control Manager push buttons and the List Manager.
 * Caption buttons, Icon Manager artwork, tracking, dimming, list selection
 * and scrollbars belong to the OS. The lavender heading is app-drawn. */
static void layout_nav(void)
{
    Rect listRect;
    Point cellSize;
    short rows = (NAV_H - 204) / 44;
    short column = (NAV_W - 16) / 3;
    if (rows < 1) rows = 1;
    if (rows > 5) rows = 5;
    if (gModelRows && rows > gModelRows) rows = gModelRows;
    SetRect(&gModelRect, 9, 112, NAV_W - 25, 112 + rows * 22);
    gConversationTop = gModelRect.bottom + 30;
    SetRect(&gNavStatusRect, 8, 52, 8 + column - 2, 76);
    SetRect(&gNavNewChatRect, 8 + column + 2, 52, 8 + 2 * column - 2, 76);
    SetRect(&gSettingsRect, 8 + 2 * column + 2, 52, NAV_W - 8, 76);
    SetRect(&listRect, 9, gConversationTop, NAV_W - 25, NAV_H - 32);
    if (gReconnectBtn) {
        MoveControl(gReconnectBtn, gNavStatusRect.left, gNavStatusRect.top);
        SizeControl(gReconnectBtn, column - 2, 24);
        MoveControl(gClearBtn, gNavNewChatRect.left, gNavNewChatRect.top);
        SizeControl(gClearBtn, column - 4, 24);
        MoveControl(gSettingsBtn, gSettingsRect.left, gSettingsRect.top);
        SizeControl(gSettingsBtn, gSettingsRect.right - gSettingsRect.left, 24);
    }
    if (gNavList) {
        OffsetRect(&(*gNavList)->rView, 0, listRect.top - (*gNavList)->rView.top);
        if ((*gNavList)->vScroll) MoveControl((*gNavList)->vScroll, listRect.right, listRect.top);
        LSize(listRect.right - listRect.left,
              listRect.bottom - listRect.top, gNavList);
        cellSize.h = listRect.right - listRect.left; cellSize.v = 22;
        LCellSize(cellSize, gNavList);
        if ((*gNavList)->vScroll) {
            MoveControl((*gNavList)->vScroll, listRect.right, listRect.top - 20);
            SizeControl((*gNavList)->vScroll, 16, listRect.bottom - listRect.top + 20);
        }
    }
    if (gModelList) {
        LSize(gModelRect.right - gModelRect.left, gModelRect.bottom - gModelRect.top, gModelList);
        cellSize.h = gModelRect.right - gModelRect.left; cellSize.v = 22;
        LCellSize(cellSize, gModelList);
        if ((*gModelList)->vScroll) {
            MoveControl((*gModelList)->vScroll, gModelRect.right, gModelRect.top - 20);
            SizeControl((*gModelList)->vScroll, 16, gModelRect.bottom - gModelRect.top + 20);
        }
    }
}

static void create_nav_controls(void)
{
    Rect view, bounds;
    Point cellSize = {22, 0}, cell = {0, 0};
    ControlFontStyleRec font;
    SetPort(gNavWin);
    layout_nav();
    gReconnectBtn = NewControl(gNavWin, &gNavStatusRect, "\pReconnect", TRUE,
                              0, 0, 1, pushButProc, 0);
    gClearBtn = NewControl(gNavWin, &gNavNewChatRect, "\pNew Chat", TRUE,
                          0, 0, 1, pushButProc, 0);
    gSettingsBtn = NewControl(gNavWin, &gSettingsRect, "\pSettings", TRUE,
                             0, 0, 1, pushButProc, 0);
    memset(&font, 0, sizeof font);
    font.flags = kControlUseFontMask | kControlUseSizeMask | kControlUseFaceMask;
    font.font = kFontIDGeneva; font.size = 10; font.style = 0;
    if (gReconnectBtn) SetControlFontStyle(gReconnectBtn, &font);
    if (gClearBtn) SetControlFontStyle(gClearBtn, &font);
    if (gSettingsBtn) SetControlFontStyle(gSettingsBtn, &font);
    TextFont(kFontIDGeneva); TextSize(12); TextFace(0);
    SetRect(&view, 9, gConversationTop, NAV_W - 25, NAV_H - 32);
    SetRect(&bounds, 0, 0, 1, 1);
    gNavList = LNew(&view, &bounds, cellSize, 0, gNavWin,
                   TRUE, FALSE, FALSE, TRUE);
    if (gNavList) {
        (*gNavList)->selFlags = lOnlyOne;
        LSetCell("New conversation", 16, cell, gNavList);
        LSetSelect(TRUE, cell, gNavList);
    }
    SetRect(&bounds, 0, 0, 1, 1);
    gModelList = LNew(&gModelRect, &bounds, cellSize, 0, gNavWin,
                     TRUE, FALSE, FALSE, TRUE);
    if (gModelList) {
        (*gModelList)->selFlags = lOnlyOne;
        LSetCell("Connecting...", 13, cell, gModelList);
    }
    layout_nav();
    SetPort(gWin);
}

static void draw_nav(void)
{
    Rect r, dot;
    const char *status = gConnected ? "Connected" :
                         (gConnPhase == 1 ? "Connecting..." : "Disconnected");
    SetRect(&r, 0, 0, NAV_W, NAV_H);
    PenNormal(); TextMode(srcCopy); fillc(&r, C_SURROUND);
    SetRect(&r, 8, 92, NAV_W - 8, 112);
    fillc(&r, C_LAV_HDR); setfg(C_TEXT);
    TextFont(gHeadingFont); TextSize(12); TextFace(gHeadingFace);
    MoveTo(14, 107); DrawString("\pModels");
    SetRect(&r, 8, 92, NAV_W - 8, gModelRect.bottom + 1);
    setfg(C_BTN_EDGE); FrameRect(&r);
    if (gModelList) {
        Rect paper = (*gModelList)->rView;
        fillc(&paper, C_BG); setfg(C_TEXT); setbg(C_BG);
        TextFont(kFontIDGeneva); TextSize(12); TextFace(0);
        /* The selected contact identifies the open chat even when another
         * window has focus. Input eligibility is checked by the event handler. */
        if (!(*gModelList)->lActive) LActivate(TRUE, gModelList);
        LUpdate(gNavWin->visRgn, gModelList);
        /* Keep native scrollbars visible, matching the transcript.
         * Only a list that fits completely is dimmed. */
        if ((*gModelList)->vScroll) {
            ShowControl((*gModelList)->vScroll);
            HiliteControl((*gModelList)->vScroll,
                gModelRows * 22 > paper.bottom - paper.top ? 0 : 255);
        }
    }
    SetRect(&r, 8, gConversationTop - 20, NAV_W - 8, gConversationTop);
    fillc(&r, C_LAV_HDR); setfg(C_TEXT);
    TextFont(gHeadingFont); TextSize(12); TextFace(gHeadingFace);
    MoveTo(14, gConversationTop - 5); DrawString("\pConversations");
    SetRect(&r, 8, gConversationTop - 20, NAV_W - 8, NAV_H - 31);
    setfg(C_BTN_EDGE); FrameRect(&r);
    if (gNavList) {
        Rect paper = (*gNavList)->rView;
        RgnHandle listClip = NewRgn();
        GetClip(listClip);
        fillc(&paper, C_BG);
        setfg(C_TEXT); setbg(C_BG);
        TextFont(kFontIDGeneva); TextSize(12); TextFace(0);
        /* Keep the current conversation highlighted across focus changes. */
        if (!(*gNavList)->lActive) LActivate(TRUE, gNavList);
        LUpdate(gNavWin->visRgn, gNavList);
        if ((*gNavList)->vScroll) {
            ShowControl((*gNavList)->vScroll);
            HiliteControl((*gNavList)->vScroll,
                gNavRows * 22 > paper.bottom - paper.top ? 0 : 255);
        }
        SetClip(listClip); DisposeRgn(listClip);
    }
    setfg(C_TEXT); setbg(C_SURROUND);
    if (gReconnectBtn) HiliteControl(gReconnectBtn, gBusy || gHistoryLoading ? 255 : 0);
    if (gClearBtn) HiliteControl(gClearBtn, !gConnected || gBusy || gHistoryLoading ? 255 : 0);
    if (gSettingsBtn) HiliteControl(gSettingsBtn, gBusy || gHistoryLoading ? 255 : 0);
    PenNormal(); TextMode(srcCopy);
    DrawControls(gNavWin);
    ForeColor(blackColor); BackColor(whiteColor);
    {
        short i;
        for (i = 0; i < 3; i++) {
            Handle icon = GetIcon(132 + i);
            Rect box;
            Rect button = i == 2 ? gSettingsRect : i ? gNavNewChatRect : gNavStatusRect;
            short center = (button.left + button.right) / 2;
            SetRect(&box, center - 16, 12, center + 16, 44);
            IconTransformType transform = (gBusy || gHistoryLoading || (i == 1 && !gConnected))
                ? kTransformDisabled : kTransformNone;
            if (PlotIconID(&box, kAlignNone, transform, 132 + i) != noErr && icon) PlotIcon(&box, icon);
        }
    }
    MoveTo(8, NAV_H - 24); LineTo(NAV_W - 20, NAV_H - 24);
    SetRect(&dot, 10, NAV_H - 16, 18, NAV_H - 8);
    setfg(gConnected ? C_OK : C_BAD); PaintOval(&dot);
    setfg(C_TEXT2); setbg(C_SURROUND);
    TextFont(kFontIDGeneva); TextSize(11); TextFace(0);
    MoveTo(24, NAV_H - 8); DrawText(status, 0, strlen(status));
    draw_grow_icon(gNavWin);
    setfg(C_TEXT); setbg(C_BG);
}

/* Real actions behind the nav strip (also reused by the Chat menu). */
static void nav_reconnect(void)
{
    if (gBusy) return;
    gAutoConnect = TRUE;
    gLastTry = 0;
    close_conn();                     /* includes an outstanding connect/search */
    set_status("reconnecting...");
    gNavDirty = TRUE;
    try_connect();
    redraw();
}
static void nav_new_chat(void)
{
    if (gBusy) return;
    gLineCount = 0; gMsgSeq = 0; gCurLen = 0; pendN = 0;
    gScroll = 0; gDocHeight = 0; gFollow = TRUE;
    gSelectedMessage = gFindRow = -1; clear_text_selection();
    /* Clear View clears loaded text; the upstream session remains active. */
    gDirty = TRUE;
    redraw();
}

static void redraw_nav(void)
{
#ifndef AGENTCHAT_IDLE_BASELINE
    if (gNavWin && EmptyRgn(gNavWin->visRgn)) return;
#endif
    if (gNavWin && gNavVisible) {
        SetPort(gNavWin);
        draw_nav();
        SetPort(gWin);
    }
    gNavDirty = FALSE;
}

static void redraw(void)
{
    gDirty = gTranscriptDirty = gStatusDirty = FALSE; gWindowFrames++;
    SetPort(gWin);   /* chat drawing always targets the chat window's port    */
    {
        Rect all = {0, 0, WIN_H, WIN_W};
        RgnHandle clip = NewRgn(), surround = NewRgn(), transcript = NewRgn();
        GetClip(clip); RectRgn(surround, &all); RectRgn(transcript, &gTxRect);
        DiffRgn(surround, transcript, surround); SectRgn(clip, surround, surround);
        SetClip(surround); PenNormal(); fillc(&all, C_SURROUND); SetClip(clip);
        DisposeRgn(transcript); DisposeRgn(surround); DisposeRgn(clip);
    }
    draw_transcript();
    {
        Rect frame = {TX_Y - 1, TX_X - 1, TX_B + 1, SB_R + 1};
        setfg(C_BTN_EDGE); FrameRect(&frame);
    }
    sync_scroll();
    /* Toolbar band is filled first so the native controls draw ON TOP of it
     * (the Send push button sits inside the band's right end). */
    draw_toolbar();
    /* Native Send push button: HiliteControl 0 = active/normal,
     * 255 = inactive (drawn dimmed). Dim while disconnected or busy.
     * TrackControl would drive this itself during tracking, but we set the
     * resting state here so the button greys out correctly between events. */
    if (gSendBtn) {
        Str255 oldTitle; StringPtr title = ToolQuestionsPending() ? "\pAnswer" : "\pSend";
        GetControlTitle(gSendBtn,oldTitle);
        /* SetControlTitle invalidates the control even for an identical title.
         * Doing that during every update makes an endless repaint cycle. */
        if (oldTitle[0] != title[0] || memcmp(oldTitle+1,title+1,title[0])) SetControlTitle(gSendBtn,title);
    }
    if (gSendBtn) HiliteControl(gSendBtn, (gConnected && (ToolQuestionsPending() || (!gBusy && !gHistoryLoading && !ChatImagesUploading() &&
        (draft_has_text() || gDraftImage[0]) && (!gDraftImage[0] || (gModelSupportsImages && ChatImagesReady(gDraftImage)))))) ? 0 : 255);
    if (gStopBtn) HiliteControl(gStopBtn, gConnected && gBusy ? 0 : 255);
    setfg(C_TEXT); setbg(C_SURROUND);
    TextFont(kFontIDGeneva); TextSize(12); TextFace(0);
    draw_input_field();
    DrawControls(gWin);     /* native scroll bar, Send and image Remove button */
    draw_status_strip();
    draw_grow_icon(gWin);
}

static void paint_changes(void)
{
#ifndef AGENTCHAT_IDLE_BASELINE
    /* Retain dirty flags; the native update/resume event paints exposed pixels. */
    if (EmptyRgn(gWin->visRgn)) return;
#endif
    if (gDirty) redraw();
    else {
        SetPort(gWin);
        if (gTranscriptDirty) { draw_transcript(); sync_scroll(); }
        if (gStatusDirty) { gStatusDirty = FALSE; draw_status_strip(); }
    }
}

/* Shared geometry for drawing, hit testing, native controls and TextEdit. */
static void layout_chat(void)
{
    Rect tr;
    SetRect(&gTxRect, TX_X, TX_Y, TX_R, TX_B);
    SetRect(&gInRect, IN_X, IN_Y, IN_R, IN_B);
    SetRect(&gBtnRect, BTN_L, BTN_T, BTN_R, BTN_B);
    if (gScrollBar) {
        MoveControl(gScrollBar, SB_L, TX_Y);
        SizeControl(gScrollBar, 16, TX_B - TX_Y);
    }
    if (gSendBtn) MoveControl(gSendBtn, BTN_L, BTN_T);
    if (gStopBtn) MoveControl(gStopBtn, BTN_L, IN_Y + 8);
    if (gTE) {
        tr = gInRect; InsetRect(&tr, 6, 4);
        if (gDraftImage[0]) tr.left += 94;
        (*gTE)->viewRect = tr;
        (*gTE)->destRect = tr;
        TECalText(gTE); TESelView(gTE);
    }
    if (gRemoveImageBtn) {
        MoveControl(gRemoveImageBtn, gInRect.left + 5, gInRect.top + 44);
        if (gDraftImage[0]) ShowControl(gRemoveImageBtn); else HideControl(gRemoveImageBtn);
        HiliteControl(gRemoveImageBtn, gBusy || gHistoryLoading ? 255 : 0);
    }
}

static long rows_height(void)
{
    long n = gLineCount < MAXLINES ? gLineCount : MAXLINES, h = 0, j;
    for (j = gLineCount - n; j < gLineCount; j++) h += gLineH[j % MAXLINES];
    return h;
}

static void reflow_transcript(long oldViewH)
{
    typedef struct { char text[LINEBUF], time[8]; unsigned char meta, brk;
                     short height; } SavedRow;
    static char body[PENDING_LIMIT];
    long n = gLineCount < MAXLINES ? gLineCount : MAXLINES;
    long base = gLineCount - n, j, k, top = 0, anchor = -1, anchorTop = 0;
    long oldTop = gDocHeight - gScroll - oldViewH, inside = 0;
    SavedRow *saved = (SavedRow *)NewPtr(n * sizeof(SavedRow));
    if (n && !saved) { SysBeep(3); return; }
    gSelectedMessage = gFindRow = -1; clear_text_selection();
    for (j = 0; j < n; j++) {
        long idx = (base + j) % MAXLINES;
        memcpy(saved[j].text, gLines[idx], LINEBUF);
        memcpy(saved[j].time, gLineTime[idx], 8);
        saved[j].meta = gLineMeta[idx]; saved[j].brk = gLineBreak[idx];
        saved[j].height = gLineH[idx];
        if ((saved[j].meta & 4) && top <= oldTop) {
            anchor = j; inside = oldTop - top;
        }
        top += saved[j].height;
    }
    gLineCount = 0;
    for (j = 0; j < n;) {
        int kind = saved[j].meta & 3, tint = (saved[j].meta >> 3) & 1;
        if (j == anchor) anchorTop = rows_height();
        if (saved[j].meta & 16) { push_image_row(saved[j].text, kind, tint); j++; continue; }
        if (!kind) {
            push_line_meta(saved[j].text, 0, 1, 0, saved[j].time); j++; continue;
        }
        if (saved[j].meta & 4) {
            push_line_meta(saved[j].text, kind, 1, tint, saved[j].time); j++;
        }
        k = 0;
        while (j < n && !(saved[j].meta & (4 | 16)) && (saved[j].meta & 3) == kind) {
            long len = strlen(saved[j].text);
            if (k + len + 2 >= PENDING_LIMIT) {
                body[k] = 0; push_body_wrapped(body, kind, tint, ""); k = 0;
            }
            memcpy(body + k, saved[j].text, len); k += len;
            if (saved[j].brk == 0) body[k++] = ' ';
            if (saved[j].brk == 1) body[k++] = '\r';
            j++;
        }
        body[k] = 0;
        push_body_wrapped(body, kind, tint, "");
    }
    if (saved) DisposePtr((Ptr)saved);
    gDocHeight = rows_height();
    if (!gFollow) gScroll = gDocHeight - anchorTop - inside - tx_viewh();
    if (gScroll < 0) gScroll = 0;
    if (gScroll > scroll_max()) gScroll = scroll_max();
}

/* Import the system scrap, sanitize it just like keyboard input, and reject
 * over-limit pastes as a whole. No silent truncation or high-bit TE input. */
static void paste_draft(void)
{
    char text[DRAFT_LIMIT];
    long len, n = 0, i;
    Handle scrap;
    if (ChatImagesPaste(gDraftImage)) { layout_chat(); cache_draft(); gDirty = TRUE; return; }
    if (TEFromScrap() != noErr) return;
    scrap = TEScrapHandle();
    len = TEGetScrapLength();
    if (len > DRAFT_LIMIT) { SysBeep(3); return; }
    HLock(scrap);
    for (i = 0; i < len; i++) {
        unsigned char c = (unsigned char)(*scrap)[i];
        if (c == '\n' && i > 0 && (*scrap)[i - 1] == '\r') continue;
        if (c == '\n') c = '\r';
        if (c == '\t') c = ' ';
        text[n++] = (c == '\r' || (c >= ' ' && c <= '~')) ? c : '?';
    }
    HUnlock(scrap);
    if ((*gTE)->teLength - ((*gTE)->selEnd - (*gTE)->selStart) + n > DRAFT_LIMIT) {
        SysBeep(3); return;
    }
    TEDelete(gTE); TEInsert(text, n, gTE); TESelView(gTE);
}

static short model_index(const char *id, Boolean create)
{
    short i;
    if (!id[0]) return -1;
    for (i = 0; i < gModelCount; i++) if (!strcmp(gModels[i].id, id)) return i;
    if (!create || gModelCount >= MODEL_CACHE) return -1;
    i = gModelCount++;
    memset(&gModels[i], 0, sizeof gModels[i]);
    strncpy(gModels[i].id, id, sizeof gModels[i].id - 1);
    return i;
}

static char *new_draft(void)
{
    short i = model_index(gModelID, FALSE);
    return i >= 0 ? gModels[i].draft : gNewDraft;
}

static char *new_image(void)
{
    short i = model_index(gModelID, FALSE);
    return i >= 0 ? gModels[i].image : gNewImage;
}

static void image_notice(const char *message)
{
    if (message) set_status(!strcmp(message, "Picture ready") && !gModelSupportsImages ? "This model does not accept pictures" : message);
    gDirty = TRUE;
}

static void select_model_list(void)
{
    short row;
    Point cell = {0, 0};
    GrafPtr saved;
    if (!gModelList) return;
    GetPort(&saved); SetPort(gNavWin);
    for (row = 0; row < gModelRows; row++) {
        cell.v = row;
        LSetSelect(!strcmp(gModels[gModelMap[row]].id, gModelID), cell, gModelList);
    }
    SetPort(saved);
}

static void model_line(char tag, char *payload)
{
    GrafPtr saved;
    Point cell = {0, 0};
    GetPort(&saved); SetPort(gNavWin);
    if (tag == 'F') {
        if (!strcmp(payload, "BEGIN")) {
            gModelBuilding = TRUE; gModelRows = 0;
            LSetDrawingMode(FALSE, gModelList); LDelRow(0, 0, gModelList);
        } else if (!strncmp(payload, "ITEM\t", 5)) {
            char *id = payload + 5, *name = strchr(id, '\t');
            if (name && gModelRows < MODEL_CACHE) {
                short i;
                *name++ = 0; i = model_index(id, TRUE);
                if (i >= 0) {
                    strncpy(gModels[i].name, name, sizeof gModels[i].name - 1);
                    cell.v = gModelRows; gModelMap[gModelRows++] = i;
                    LAddRow(1, cell.v, gModelList);
                    LSetCell(gModels[i].name, strlen(gModels[i].name), cell, gModelList);
                }
            }
        } else if (!strcmp(payload, "END")) {
            gModelBuilding = FALSE; layout_nav();
            LSetDrawingMode(TRUE, gModelList); select_model_list();
        }
    } else {
        char *name = strchr(payload, '\t');
        char *cap = name ? strchr(name + 1, '\t') : NULL;
        short i;
        gModelSupportsImages = cap && atoi(cap + 1) != 0;
        if (cap) *cap = 0;
        if (name) *name++ = 0;
        strncpy(gModelID, payload, sizeof gModelID - 1);
        strncpy(gModelName, name ? name : payload, sizeof gModelName - 1);
        i = model_index(gModelID, TRUE);
        if (i >= 0) strncpy(gModels[i].name, gModelName, sizeof gModels[i].name - 1);
        strncpy(gPrefs.model, gModelID, sizeof gPrefs.model - 1); save_preferences();
        update_chat_title();
        select_model_list();
    }
    SetPort(saved); gNavDirty = gDirty = TRUE;
}

static short chat_index(const char *id, Boolean create)
{
    short i, empty = -1;
    if (!id[0]) return -1;
    for (i = 0; i < gChatCount; i++) {
        if (strcmp(gChats[i].id, id) == 0) return i;
        if (!gChats[i].id[0] && empty < 0) empty = i;
    }
    if (!create || (empty < 0 && gChatCount >= HISTORY_CACHE)) return -1;
    i = empty >= 0 ? empty : gChatCount++;
    memset(&gChats[i], 0, sizeof gChats[i]);
    strncpy(gChats[i].id, id, sizeof gChats[i].id - 1);
    return i;
}

static void cache_draft(void)
{
    short i = chat_index(gActiveID, TRUE);
    char *draft = i >= 0 ? gChats[i].draft : new_draft();
    long n = gTE ? (*gTE)->teLength : 0;
    if (n > DRAFT_LIMIT) n = DRAFT_LIMIT;
    if (gTE) { HLock((*gTE)->hText); memcpy(draft, *(*gTE)->hText, n); HUnlock((*gTE)->hText); }
    draft[n] = 0;
    strcpy(i >= 0 ? gChats[i].image : new_image(), gDraftImage);
}

static void select_nav_id(void)
{
    short row;
    Point cell = {0, 0};
    GrafPtr saved;
    if (!gNavList) return;
    GetPort(&saved); SetPort(gNavWin);
    for (row = 0; row < gNavRows; row++) {
        cell.v = row;
        LSetSelect(row == 0 ? !gActiveID[0] :
                   strcmp(gChats[gNavMap[row]].id, gActiveID) == 0, cell, gNavList);
    }
    SetPort(saved);
}

static void request_conversation(const char *id)
{
    char request[104];
    if (!gConnected || gBusy || gHistoryLoading || ChatImagesUploading()) { set_status("Wait for the current operation"); return; }
    cache_draft(); gDraftCached = TRUE;
    if (!id[0]) { new_draft()[0] = 0; new_image()[0] = 0; }
    snprintf(request, sizeof request, id[0] ? "O:%s" : "N:%s", id);
    gHistoryLoading = TRUE;
    set_status("loading conversation...");
    send_line(request);
    gDirty = gNavDirty = TRUE;
}

static void request_model(const char *id)
{
    char request[140];
    if (!gConnected || gBusy || gHistoryLoading || ChatImagesUploading() || !strcmp(gModelID, id)) return;
    cache_draft(); gDraftCached = TRUE; gHistoryLoading = TRUE;
    snprintf(request, sizeof request, "M:%s", id); send_line(request);
    set_status("opening contact..."); gDirty = gNavDirty = TRUE;
}

static void history_line(char tag, char *payload)
{
    if (tag == 'Z') {
        short i = chat_index(payload, FALSE);
        if (i >= 0) memset(&gChats[i], 0, sizeof gChats[i]);
        if (strcmp(gActiveID, payload) == 0) {
            gActiveID[0] = gPrefs.lastID[0] = 0;
            save_preferences();
            /* Do not cache the deleted field into the New Chat draft. */
            gDraftCached = TRUE;
        }
    } else if (tag == 'J') {
        GrafPtr saved;
        Point cell = {0, 0};
        GetPort(&saved); SetPort(gNavWin);
        if (strcmp(payload, "BEGIN") == 0) {
            gListBuilding = TRUE; gNavRows = 1;
            LSetDrawingMode(FALSE, gNavList); LDelRow(0, 0, gNavList); LAddRow(1, 0, gNavList);
            LSetCell("New conversation", 16, cell, gNavList);
        } else if (strncmp(payload, "ITEM\t", 5) == 0) {
            char *id = payload + 5, *title = strchr(id, '\t');
            if (title && gNavRows <= HISTORY_CACHE) {
                short i;
                *title++ = 0; i = chat_index(id, TRUE);
                if (i >= 0) {
                    strncpy(gChats[i].title, title, sizeof gChats[i].title - 1);
                    cell.v = gNavRows; gNavMap[gNavRows++] = i;
                    LAddRow(1, cell.v, gNavList);
                    LSetCell(gChats[i].title, strlen(gChats[i].title), cell, gNavList);
                }
            }
        } else if (strcmp(payload, "END") == 0) {
            gListBuilding = FALSE; LSetDrawingMode(TRUE, gNavList); select_nav_id();
        }
        SetPort(saved); gNavDirty = TRUE;
    } else if (tag == 'K') {
        Boolean restore = FALSE;
        if (strcmp(gActiveID, payload) != 0) {
            short i;
            restore = payload[0] && !gBusy && !gHistoryLoading && !gLineCount;
            cache_draft();
            strncpy(gActiveID, payload, sizeof gActiveID - 1);
            i = chat_index(gActiveID, TRUE);
            if (i >= 0 && new_draft()[0] && !gChats[i].draft[0]) strcpy(gChats[i].draft, new_draft());
            if (i >= 0 && new_image()[0] && !gChats[i].image[0]) strcpy(gChats[i].image, new_image());
        }
        if (strcmp(gPrefs.lastID, payload) != 0) {
            strncpy(gPrefs.lastID, payload, sizeof gPrefs.lastID - 1); save_preferences();
        }
        select_nav_id(); gNavDirty = TRUE;
        /* A bridge can resume an existing context even without a local
         * bookmark (for example after changing its port). Load that selected
         * conversation instead of displaying an empty view for its real ID. */
        if (restore) request_conversation(gActiveID);
    } else if (tag == 'V') {
        if (strncmp(payload, "BEGIN\t", 6) == 0) {
            short i;
            char *clock = strchr(payload + 6, '\t');
            unsigned long local = 0;
            gHistoryTimeSet = FALSE;
            if (clock) {
                *clock++ = 0;
                GetDateTime(&local);
                if (local && clock[0]) {
                    gHistoryTimeDelta = (long)(local - (strtoul(clock, NULL, 10) + 2082844800UL));
                    gHistoryTimeSet = TRUE;
                }
            }
            if (!gDraftCached) cache_draft();
            gDraftCached = FALSE;
            strncpy(gActiveID, payload + 6, sizeof gActiveID - 1);
            i = chat_index(gActiveID, TRUE);
            gLineCount = gMsgSeq = gCurLen = pendN = 0;
            gScroll = gDocHeight = 0; gFollow = TRUE;
            gSelectedMessage = gFindRow = -1; clear_text_selection();
            gHistoryLoading = TRUE;
            SetPort(gWin);
            TESetText(i >= 0 ? gChats[i].draft : new_draft(),
                      strlen(i >= 0 ? gChats[i].draft : new_draft()), gTE);
            TESetSelect((*gTE)->teLength, (*gTE)->teLength, gTE); TESelView(gTE);
            strcpy(gDraftImage, i >= 0 ? gChats[i].image : new_image()); layout_chat();
        } else if (strncmp(payload, "MESSAGE\t", 8) == 0) {
            char *stamp = strchr(payload + 8, '\t');
            char *name = stamp ? strchr(stamp + 1, '\t') : NULL;
            flush_cur();
            gPendKind = atoi(payload + 8); gPendFirst = 1;
            gNeedAgentTag = FALSE;
            gPendTint = (int)(gMsgSeq++ & 1);
            gPendTime[0] = 0;
            if (name) *name++ = 0;
            strncpy(gPendName, name && name[0] ? name : gModelName, sizeof gPendName - 1);
            if (stamp) history_hhmm(gPendTime, stamp + 1);
            cur_append(gPendKind == 1 ? "You: " : "Agent: ", gPendKind == 1 ? 5 : 7);
        } else if (strncmp(payload, "TEXT\t", 5) == 0) {
            unescape_append(payload + 5, strlen(payload + 5));
        } else if (strcmp(payload, "DONE") == 0) {
            flush_cur();
        } else if (strcmp(payload, "END") == 0) {
            flush_cur(); gHistoryLoading = FALSE;
            set_status("ready");
        } else if (strncmp(payload, "ERROR\t", 6) == 0) {
            gHistoryLoading = gDraftCached = FALSE;
            set_status(payload + 6); select_nav_id(); select_model_list();
        }
        gDirty = gNavDirty = TRUE;
    }
}

/* ---------------- native Balloon Help ---------------- */
/* Menus and modal dialogs use hmnu/hdlg resources. These two resizable
 * document windows need dynamic hot rectangles (Inside Macintosh, Help
 * Manager, pp. 3-74 and 3-100). Never force the system setting on: the user
 * chooses Show/Hide Balloons in Apple's Help menu. */
static short gHelpIndex = 0;
static WindowPtr gHelpWindow = NULL;
static WindowPtr gHelpBalloonWindow = NULL;
static Rect gHelpRect;
static UInt32 gHelpSince = 0;
static Boolean gHelpShown = FALSE;

static void dismiss_help(void)
{
    GrafPtr saved;
    GetPort(&saved);
    /* Only remove a balloon we displayed, leaving system/menu help alone. */
    if (gHelpShown && HMIsBalloon()) HMRemoveBalloon();
    gHelpShown = FALSE;
    gHelpIndex = 0;
    gHelpWindow = NULL;
    gHelpBalloonWindow = NULL;
    SetPort(saved);
}

static short window_help(WindowPtr window, Point mouse, Rect *hot)
{
    if (window == gWin) {
        *hot = gBtnRect;
        if (PtInRect(mouse, hot)) {
            if (!gConnected) return kHelpSendOffline;
            if (gBusy) return kHelpSendBusy;
            if (gHistoryLoading) return kHelpLoading;
            if (ChatImagesUploading() || (gDraftImage[0] && !ChatImagesReady(gDraftImage))) return kHelpPictureUpload;
            if (gDraftImage[0] && !gModelSupportsImages) return kHelpPictureUnsupported;
            return draft_has_text() || gDraftImage[0] ? kHelpSend : kHelpSendEmpty;
        }
        *hot = (**gStopBtn).contrlRect;
        if (PtInRect(mouse, hot)) return gConnected && gBusy ? kHelpStop : kHelpStopIdle;
        if (gDraftImage[0]) {
            *hot = (**gRemoveImageBtn).contrlRect;
            if (PtInRect(mouse, hot)) return kHelpPictureRemove;
            SetRect(hot, gInRect.left + 5, gInRect.top + 4, gInRect.left + 88, gInRect.top + 43);
            if (PtInRect(mouse, hot)) return kHelpPicturePreview;
        }
        *hot = gInRect;
        if (PtInRect(mouse, hot)) return kHelpComposer;
        *hot = gTxRect;
        if (PtInRect(mouse, hot)) return kHelpTranscript;
        *hot = (**gScrollBar).contrlRect;
        if (PtInRect(mouse, hot)) return kHelpTranscriptScroll;
        SetRect(hot, TX_X, TOOL_T, BTN_L - 8, TOOL_B);
        if (PtInRect(mouse, hot)) return kHelpFormatting;
        SetRect(hot, TX_X, ST_Y - 2, WIN_W - 20, WIN_H);
        if (PtInRect(mouse, hot)) return kHelpStatus;
    } else if (window == gNavWin && gNavVisible) {
        *hot = gNavStatusRect; hot->top = 12; /* Include native Icon Manager art. */
        if (PtInRect(mouse, hot)) return gBusy || gHistoryLoading ? kHelpActionBusy : kHelpReconnect;
        *hot = gNavNewChatRect; hot->top = 12;
        if (PtInRect(mouse, hot)) {
            if (gBusy || gHistoryLoading) return kHelpActionBusy;
            return gConnected ? kHelpNewChat : kHelpNewChatOffline;
        }
        *hot = gSettingsRect; hot->top = 12;
        if (PtInRect(mouse, hot)) return gBusy || gHistoryLoading ? kHelpActionBusy : kHelpSettings;
        if (gModelList) {
            *hot = (**gModelList).rView;
            if (PtInRect(mouse, hot)) return kHelpModels;
            if ((**gModelList).vScroll) {
                *hot = (**(**gModelList).vScroll).contrlRect;
                if (PtInRect(mouse, hot)) return kHelpModelsScroll;
            }
        }
        if (gNavList) {
            *hot = (**gNavList).rView;
            if (PtInRect(mouse, hot)) return gHistoryLoading ? kHelpLoading : kHelpHistory;
            *hot = (**(**gNavList).vScroll).contrlRect;
            if (PtInRect(mouse, hot)) return kHelpHistoryScroll;
        }
        SetRect(hot, 8, NAV_H - 30, NAV_W - 20, NAV_H);
        if (PtInRect(mouse, hot)) return kHelpStatus;
    }
    return 0;
}

static void idle_help(Point globalMouse)
{
    GrafPtr saved;
    WindowPtr window = NULL;
    Point mouse = globalMouse, tip, corner;
    Rect hot;
    short index = 0;
    UInt32 now = TickCount();
    HMMessageRecord message;
    OSErr err;
    if (!HMGetBalloons()) { dismiss_help(); return; }
    GetPort(&saved);
    if (FrontWindow() == gWin || FrontWindow() == gNavWin) {
        short part = FindWindow(globalMouse, &window);
        /* A large area's balloon may cover the pointer. It is a real floating
         * window, so FindWindow sees the balloon instead of its owner. Keep
         * tracking the owner's hot rectangle; otherwise idle removes and
         * reopens the same balloon every half second.
         * The Help Manager removes it when the pointer leaves the rectangle. */
        if (gHelpShown && window == gHelpBalloonWindow &&
            PtInRect(globalMouse, &gHelpRect)) window = gHelpWindow;
        if (part == inContent && (window == gWin || window == gNavWin)) {
            SetPort(window); GlobalToLocal(&mouse);
            index = window_help(window, mouse, &hot);
        }
    }
    if (!index) { dismiss_help(); SetPort(saved); return; }
    /* Compare geometry too: grow/zoom and dragging must never leave stale tips. */
    corner.h = hot.left; corner.v = hot.top; LocalToGlobal(&corner);
    tip.h = hot.right; tip.v = hot.bottom; LocalToGlobal(&tip);
    SetRect(&hot, corner.h, corner.v, tip.h, tip.v);
    if (index != gHelpIndex || window != gHelpWindow || !EqualRect(&hot, &gHelpRect)) {
        dismiss_help();
        gHelpIndex = index; gHelpWindow = window; gHelpRect = hot;
        gHelpSince = now;
    }
    if (!gHelpShown && (UInt32)(now - gHelpSince) >= 30 && !HMIsBalloon()) {
        memset(&message, 0, sizeof message);
        message.hmmHelpType = kHMStringResItem;
        message.u.hmmStringRes.hmmResID = kAgentHelpStrings;
        message.u.hmmStringRes.hmmIndex = index;
        tip = globalMouse;
        err = HMShowBalloon(&message, tip, &gHelpRect, NULL, 0, 0, kHMRegularWindow);
        if (err == noErr) {
            gHelpShown = TRUE;
            HMGetBalloonWindow(&gHelpBalloonWindow);
        }
        else gHelpSince = now; /* Retry later if the pointer was still moving. */
    }
    SetPort(saved);
}

/* ---------------- menu ---------------- */
static MenuHandle gFileMenu = NULL;
static MenuHandle gEditMenu = NULL;
static MenuHandle gWindowMenu = NULL;

static void build_menu(void)
{
    MenuHandle apple;
    apple = NewMenu(1, "\p\024");   /* Apple menu (was missing entirely) */
    InsertMenuItem(apple, "\pAbout AgentChat\311", 1);
    InsertMenu(apple, 0);
    AppendResMenu(apple, 'DRVR');
    gFileMenu = NewMenu(2, "\pFile");
    InsertMenuItem(gFileMenu, "\pSave Transcript\311", 1);
    InsertMenuItem(gFileMenu, "\p-", 2);
    InsertMenuItem(gFileMenu, "\pQuit", 3);
    InsertMenuItem(gFileMenu, "\p-", 4);
    InsertMenuItem(gFileMenu, "\pNew Chat", 5);
    InsertMenuItem(gFileMenu, "\pConnection Settings...", 6);
    InsertMenuItem(gFileMenu, "\pModels...", 7);
    SetItemCmd(gFileMenu, 1, 'S');
    SetItemCmd(gFileMenu, 3, 'Q');
    SetItemCmd(gFileMenu, 5, 'N');
    SetItemCmd(gFileMenu, 6, ',');
    SetItemCmd(gFileMenu, 7, 'M');
    InsertMenu(gFileMenu, 0);
    /* Edit menu RESTORED (v30): the composer is now a native TEHandle, so
     * Cut/Copy/Paste/Select All honestly work via the TextEdit Manager against
     * the system clipboard. (Removed in 2026-09-29 because the old self-drawn
     * field had no real selection; that reason no longer holds.) */
    gEditMenu = NewMenu(4, "\pEdit");
    InsertMenuItem(gEditMenu, "\pCut", 1);
    InsertMenuItem(gEditMenu, "\pCopy", 2);
    InsertMenuItem(gEditMenu, "\pPaste", 3);
    InsertMenuItem(gEditMenu, "\pSelect All", 4);
    SetItemCmd(gEditMenu, 1, 'X');
    SetItemCmd(gEditMenu, 2, 'C');
    SetItemCmd(gEditMenu, 3, 'V');
    SetItemCmd(gEditMenu, 4, 'A');
    InsertMenuItem(gEditMenu, "\p-", 5);
    InsertMenuItem(gEditMenu, "\pCopy Transcript", 6);
    SetItemCmd(gEditMenu, 6, 'C');
    SetMenuItemModifiers(gEditMenu, 6, kMenuShiftModifier);
    InsertMenuItem(gEditMenu, "\pFind...", 7);
    InsertMenuItem(gEditMenu, "\pFind Next", 8);
    SetItemCmd(gEditMenu, 7, 'F'); SetItemCmd(gEditMenu, 8, 'G');
    InsertMenu(gEditMenu, 0);
    gMenu = NewMenu(200, "\pChat");
    InsertMenuItem(gMenu, "\pConnect now", 1);
    InsertMenuItem(gMenu, "\pScreenshot", 2);
    InsertMenuItem(gMenu, "\pCheck for Update...", 3);
    InsertMenuItem(gMenu, "\pQuit", 4);
    SetItemCmd(gMenu, 1, 'K');
    SetItemCmd(gMenu, 3, 'U');
    InsertMenuItem(gMenu, "\pRefresh History", 5);
    InsertMenuItem(gMenu, "\pRename Conversation...", 6);
    InsertMenuItem(gMenu, "\pStop Reply", 7);
    SetItemCmd(gMenu, 5, 'R'); SetItemCmd(gMenu, 6, 'E'); SetItemCmd(gMenu, 7, '.');
    InsertMenuItem(gMenu, "\pDelete Conversation...", 8);
#ifdef AGENTCHAT_DEV_FIXTURE
    InsertMenuItem(gMenu, "\pLoad Test Fixture", 9);
    SetItemCmd(gMenu, 9, 'T');
#endif
    InsertMenu(gMenu, 0);
    gWindowMenu = NewMenu(300, "\pWindow");
    InsertMenuItem(gWindowMenu, "\pShow/Hide Navigation", 1);
    InsertMenuItem(gWindowMenu, "\pChat Window", 2);
    InsertMenuItem(gWindowMenu, "\pArrange Windows", 3);
    SetItemCmd(gWindowMenu, 1, 'L');
    SetItemCmd(gWindowMenu, 3, '0');
    InsertMenu(gWindowMenu, 0);
    DrawMenuBar();
}

static void update_edit_menu(void)
{
    short i;
    Boolean active = gTE && FrontWindow() == gWin && gComposerFocus;
    SetMenuItemText(gMenu, 1, gConnected ? "\pDisconnect" : "\pConnect now");
    for (i = 1; i <= 4; i++) {
        Boolean enabled = active;
        if (i <= 2) enabled = active && (*gTE)->selStart != (*gTE)->selEnd;
        if (i == 2 && FrontWindow() == gWin && !gComposerFocus && (gSelectedMessage >= 0 || has_text_selection())) enabled = TRUE;
        if (i == 4 && FrontWindow() == gWin && !gComposerFocus && gLineCount) enabled = TRUE;
        if (enabled) EnableItem(gEditMenu, i); else DisableItem(gEditMenu, i);
    }
    if (gConnected && !gBusy && !gHistoryLoading) {
        EnableItem(gFileMenu, 7); EnableItem(gFileMenu, 5); EnableItem(gFileMenu, 6); EnableItem(gMenu, 5);
        if (gActiveID[0]) { EnableItem(gMenu, 6); EnableItem(gMenu, 8); }
        else { DisableItem(gMenu, 6); DisableItem(gMenu, 8); }
    } else {
        DisableItem(gFileMenu, 7); DisableItem(gFileMenu, 5); DisableItem(gMenu, 5); DisableItem(gMenu, 6); DisableItem(gMenu, 8);
        if (gBusy || gHistoryLoading) DisableItem(gFileMenu, 6); else EnableItem(gFileMenu, 6);
    }
    if (gConnected && gBusy) EnableItem(gMenu, 7); else DisableItem(gMenu, 7);
}

static void show_about(void)
{
    DialogPtr dlg;
    short itemHit;
    dlg = GetNewDialog(131, NULL, (WindowPtr)-1);
    if (dlg == NULL) return;
    {
        DialogItemType type;
        Handle item;
        Rect rect;
        Str255 version;
        char text[48];
        snprintf(text, sizeof text, "AgentChat v%s", AGENTCHAT_VERSION);
        version[0] = strlen(text); memcpy(version + 1, text, version[0]);
        GetDialogItem(dlg, 3, &type, &item, &rect);
        SetDialogItemText(item, version);
    }
    do {
        run_modal_dialog(NULL, &itemHit);
    } while (itemHit != 1);
    DisposeDialog(dlg);
    /* v30 dialog-occlusion fix: the modal painted over the transcript and the
     * OS does not always hand the covered region back to our custom redraw,
     * leaving a blank patch under where the About box was. Force a full
     * repaint of the chat window (and nav) now that the dialog is gone. */
    redraw();
    if (gNavVisible) redraw_nav();
}

static void save_transcript(void)
{
    StandardFileReply rep;
    SInt16 fd = 0;
    long i;
    OSErr err;
    gModalDepth++;
    StandardPutFile("\pSave transcript as:", "\pAgentChat log", &rep);
    gModalDepth--;
    if (!rep.sfGood) return;
    rep.sfType = 'TEXT';
    err = FSpCreate(&rep.sfFile, APP_CREATOR, 'TEXT', 0);
    if (err != noErr && err != dupFNErr) {
        push_line("** save failed (create)");
        return;
    }
    if (FSpOpenDF(&rep.sfFile, fsRdWrCmd, &fd) != noErr) {
        push_line("** save failed (open)");
        return;
    }
    err = SetFPos(fd, fsFromStart, 0);
    if (err == noErr) err = SetEOF(fd, 0);
    for (i = 0; err == noErr && i < gLineCount && i < MAXLINES; i++) {
        long idx = (gLineCount <= MAXLINES) ? i : (gLineCount - MAXLINES + i);
        char line[LINEBUF + 2];
        const char *body = gLineMeta[idx % MAXLINES] & 16 ? "[Image]" : gLines[idx % MAXLINES];
        long n = (long)strlen(body);
        if (n > LINEBUF - 1) n = LINEBUF - 1;
        memcpy(line, body, n);
        line[n++] = '\r';
        err = FSWrite(fd, &n, line);
    }
    {
        OSErr closeErr = FSClose(fd);
        if (err == noErr) err = closeErr;
    }
    push_line(err == noErr ? "** transcript saved" : "** save failed (write)");
}

/* One dispatcher for menu bar clicks and Cmd-key equivalents. */
static void do_menu(long sel)
{
    short menu = (short)(sel >> 16);
    short item = (short)(sel & 0xFFFF);
    if (menu == 1) {
        if (item == 1) {
            show_about();
        } else {
            Str255 name;
            GetMenuItemText(GetMenuHandle(1), item, name);
            OpenDeskAcc(name);
        }
    } else if (menu == 2) {
        if (item == 1) save_transcript();
        else if (item == 3) quit_app();
        else if (item == 5) request_conversation("");
        else if (item == 6) connection_settings();
        else if (item == 7) model_settings();
    } else if (menu == 4 && item >= 6) {
        if (item == 6) copy_transcript(FALSE);
        else find_transcript(item == 7);
    } else if (menu == 4 && item == 2 && FrontWindow() == gWin && !gComposerFocus) {
        copy_transcript(TRUE);
    } else if (menu == 4 && item == 4 && FrontWindow() == gWin && !gComposerFocus) {
        SetPort(gWin); select_transcript();
    } else if (menu == 4 && FrontWindow() == gWin && gComposerFocus) {
        /* Edit — v30. Native TextEdit against the system clipboard. The
         * composer is the only editable field, so every command targets gTE.
         * With no live selection TECut/TECopy no-op (beep); TEPaste inserts
         * the clipboard's text. SetPort so the redraw lands in the chat
         * window; TEActivate makes the caret real before the op. */
        if (gTE != NULL) {
            SetPort(gWin);
            TEActivate(gTE);
            switch (item) {
                case 1: TECut(gTE); ZeroScrap(); TEToScrap(); gSystemScrapOwned = FALSE; break;
                case 2: TECopy(gTE); ZeroScrap(); TEToScrap(); gSystemScrapOwned = FALSE; break;
                case 3: paste_draft(); break;                       /* ⌘V */
                case 4: TESetSelect(0, 0x7FFFFFFFL, gTE); break;  /* ⌘A */
            }
            TEUpdate(&gInRect, gTE);
            gDirty = TRUE;
        }
    } else if (menu == 200) {
        switch (item) {
            case 1:
                if (gConnected) {
                    gAutoConnect = FALSE;
                    close_conn();
                    set_status("disconnected");
                } else {
                    gAutoConnect = TRUE;
                    gLastTry = 0;
                    try_connect();
                }
                break;
            case 2:
                do_screen();
                break;
            case 3:
                ask_update();
                break;
            case 4:
                quit_app();
                break;
            case 5: if (gConnected) { send_line("F:"); send_line("L:"); } break;
            case 6: rename_conversation(); break;
            case 7: if (gConnected && gBusy) send_line("A:"); break;
            case 8: delete_conversation(); break;
#ifdef AGENTCHAT_DEV_FIXTURE
            case 9:
                if (!gBusy) { nav_new_chat(); seed_fixture(); }
                break;
#endif
            default:
                break;
        }
    } else if (menu == 300) {
        if (item == 1) {
            if (gNavVisible) {
                HideWindow(gNavWin);
                gNavVisible = FALSE;
                SelectWindow(gWin);
            } else {
                ShowWindow(gNavWin);
                gNavVisible = TRUE;
                keep_window_visible(gNavWin);
                SetPort(gNavWin); draw_nav(); SetPort(gWin);
                SelectWindow(gWin);
            }
        } else if (item == 2) {
            keep_window_visible(gWin);
            SelectWindow(gWin);
        } else if (item == 3) arrange_windows();
    }
    HiliteMenu(0);
    redraw();
    if (gNavVisible) redraw_nav();
}

/* ---------------- update dialog ---------------- */
static pascal Boolean update_filter(DialogPtr dlg, EventRecord *ev, short *itemHit)
{
    observe_background_event(ev);
    if (ev->what == kHighLevelEvent) {
        AEProcessAppleEvent(ev);
        *itemHit = 0;
        return TRUE;
    }
    if (ev->what == mouseDown) {
        WindowPtr clicked;
        short part = FindWindow(ev->where, &clicked);
        if (part == inGoAway && clicked == (WindowPtr)dlg) {
            if (TrackGoAway(clicked, ev->where)) *itemHit = 3;
            else *itemHit = 0;
            return TRUE;
        }
        if (clicked == (WindowPtr)dlg && part == inDrag) {
            DragWindow(clicked,ev->where,&qd.screenBits.bounds); *itemHit = 0; return TRUE;
        }
        if (clicked == (WindowPtr)dlg && part == inCollapseBox) {
            if (TrackBox(clicked,ev->where,part)) CollapseWindow(clicked,!IsWindowCollapsed(clicked));
            *itemHit = 0; return TRUE;
        }
    }
    /* Default-button behavior implemented here (this SDK lacks SetDefaultItem):
     * Return = OK (item 2), Escape = Cancel (item 3). */
    if (ev->what == keyDown) {
        short ch = (short)(ev->message & charCodeMask);
        if (ch == '\r' || ch == '\n') { *itemHit = 2; return TRUE; }
        if ((ev->modifiers & cmdKey) && (ch == 'w' || ch == 'W') && ((WindowPeek)dlg)->goAwayFlag) { *itemHit = 3; return TRUE; }
        if (ch == 0x1B || ((ev->modifiers & cmdKey) && ch == '.')) {
            *itemHit = 3; return TRUE;
        }
        if ((ev->modifiers & cmdKey) && ((DialogPeek)dlg)->editField >= 0) {
            GrafPtr saved;
            Boolean handled = TRUE;
            GetPort(&saved); SetPort((GrafPtr)dlg);
            if (ch == 'a') SelectDialogItemText(dlg, ((DialogPeek)dlg)->editField + 1, 0, 32767);
            else if (ch == 'c' || ch == 'x') {
                if (ch == 'c') DialogCopy(dlg); else DialogCut(dlg);
                ZeroScrap(); TEToScrap(); gSystemScrapOwned = FALSE;
            } else if (ch == 'v') { TEFromScrap(); DialogPaste(dlg); }
            else handled = FALSE;
            SetPort(saved);
            if (handled) { *itemHit = 0; return TRUE; }
        }
    }
    if (ev->what == updateEvt && ModelSettingsEvent(ev)) return TRUE;
    if (ev->what == updateEvt &&
        ((WindowPtr)ev->message == gWin || (WindowPtr)ev->message == gNavWin)) {
        GrafPtr saved;
        WindowPtr window = (WindowPtr)ev->message;
        GetPort(&saved); SetPort(window);
        BeginUpdate(window);
        if (window == gWin) redraw();
        else if (gNavVisible) draw_nav();
        EndUpdate(window); SetPort(saved);
        return FALSE;
    }
    if (ev->what == nullEvent) {
        GrafPtr saved;
        GetPort(&saved);
        /* Service the link cooperatively. Paint only changed content: erasing
         * and repainting every idle event made the transcript flash behind
         * Rename/Find/Settings, even when no messages were arriving. */
        pump_ot_events();
        if (gConnected) { poll_recv(); q_flush(); }
        else { try_connect(); }
        paint_changes();
        if (gNavVisible && gNavDirty) redraw_nav();
        animate_status();
        ModelSettingsTick(gConnected);
        SetPort(saved);
    }
    return FALSE;
}

/* Delete our stale ".PREV" sibling left by the last replace-in-place
 * update (2026-09-29). The folder must hold exactly one live copy. */
static void cleanup_prev(void)
{
    ProcessSerialNumber psn;
    ProcessInfoRec info;
    FSSpec me;
    FSRef ref;
    FSCatalogInfo ci;
    Str255 prevNm;
    FSSpec ps;
    long L;

    GetCurrentProcess(&psn);
    memset(&info, 0, sizeof(info));
    info.processInfoLength = sizeof(info);
    info.processName = NULL;
    info.processAppSpec = &me;
    if (GetProcessInformation(&psn, &info) != noErr) return;
    L = (long)me.name[0];
    if (L <= 0 || L > 248) return;
    if (FSpMakeFSRef(&me, &ref) != noErr) return;
    if (FSGetCatalogInfo(&ref, kFSCatInfoParentDirID, &ci, NULL, NULL, false) != noErr)
        return;
    prevNm[0] = (unsigned char)(L + 5);
    memcpy(prevNm + 1, me.name + 1, L);
    memcpy(prevNm + 1 + L, ".PREV", 5);
    if (FSMakeFSSpec(me.vRefNum, ci.parentDirID, prevNm, &ps) == noErr)
        FSpDelete(&ps);
}

static void ask_update(void)
{
    DialogPtr dlg;
    ModalFilterUPP filter;
    short itemHit;
    DialogItemType type;
    Handle itemH;
    Rect box;
    Str255 def;
    char path[80];
    ProcessSerialNumber psn;
    ProcessInfoRec info;
    FSSpec me;

    if (!valid_address(gPrefs.host)) {
        set_status("connect to a relay before updating");
        return;
    }

    /* Pre-fill with OUR OWN filename + ".NEW" (2026-09-29 incident: the
     * hardcoded "\pAgentChat.NEW" matched a stale wrong-config file in the
     * filer; a user accepting the dialog default launched an app wired to
     * the production host and got a connect loop).  A trailing ".NEW" is
     * stripped first, so "Foo.NEW" still pre-fills "Foo.NEW" — not
     * "Foo.NEW.NEW" — after an update relaunches us under that name. */
    def[0] = 0;
    GetCurrentProcess(&psn);
    memset(&info, 0, sizeof(info));
    info.processInfoLength = sizeof(info);
    info.processName = NULL;
    info.processAppSpec = &me;
    if (GetProcessInformation(&psn, &info) == noErr &&
        me.name[0] > 0 && me.name[0] < 250) {
        long L = (long)me.name[0];
        if (L > 4 && memcmp(me.name + L - 3, ".NEW", 4) == 0) L -= 4;
        def[0] = (unsigned char)(L + 4);
        memcpy(def + 1, me.name + 1, (size_t)L);
        memcpy(def + 1 + L, ".NEW", 4);
    }
    if (def[0] == 0) {           /* fallback: cannot discover own name */
        def[0] = 13;
        memcpy(def + 1, "AgentChat.NEW", 13);
    }

    dlg = GetNewDialog(130, NULL, (WindowPtr)-1);
    if (dlg == NULL) return;
    /* DITL 130 numbering (see AgentChat.r): 1 = EditText, 2 = OK, 3 = Cancel. */
    GetDialogItem(dlg, 1, &type, &itemH, &box);
    if (type != editText) {
        DisposeDialog(dlg);
        fail_update("** update: dialog item 1 is not the EditText");
        return;
    }
    SetDialogItemText(itemH, def);
    SelectDialogItemText(dlg, 1, 0, 32767);
    filter = NewModalFilterUPP(update_filter);
    if (!filter) { DisposeDialog(dlg); SysBeep(3); return; }
    do {
        run_modal_dialog(filter, &itemHit);
    } while (itemHit != 2 && itemHit != 3);
    DisposeModalFilterUPP(filter);

    if (itemHit == 2) {
        GetDialogItemText(itemH, def);
        if (def[0] == 0) {
            DisposeDialog(dlg);
            fail_update("** update: empty name");
            return;
        }
        {
            long len = def[0];
            if (len >= sizeof path) len = sizeof path - 1;
            memcpy(path, def + 1, len); path[len] = 0;
        }
        DisposeDialog(dlg);
        do_update(path);
        return;
    }
    DisposeDialog(dlg);
    redraw();   /* v30 dialog-occlusion fix (update dialog cancel path) */
    if (gNavVisible) redraw_nav();
}

/* ---------------- main ---------------- */
/* Ensure OUR OWN Finder bundle bit is set (and hasBeenInited cleared) so the
 * Finder installs our 'BNDL'/'FREF' into the per-volume Desktop Database and
 * draws our custom icon instead of the generic "unknown application" page.
 *
 * Root cause of the persistent generic icon (2026-09-29): our resource fork
 * already had a correct ICN#/ics#/icl8/ics8 + BNDL(creator AGCH)/FREF('APPL'),
 * and the file was correctly typed 'APPL'/creator 'AGCH'.  But the bundle bit
 * (kHasBundle=0x2000) in the file's FInfo was CLEAR, and Apple's resolution
 * order skips the bundle entirely when that bit is unset -- so every pixel/
 * resource fix and every Cmd-Opt desktop rebuild was a no-op for the icon.
 * hfsutils cannot set it (this build has no hchmod, and `hattrib` only does
 * type/creator/locked/invisible), so we set the bundle bit on our own file at
 * launch here.  BUT this proved NECESSARY-YET-INSUFFICIENT: the bundle bit only
 * tells Finder a bundle exists; it does NOT populate the per-volume Desktop
 * Database with our BNDL/FREF/ICN#, so the icon stayed generic.  The actual
 * fix is the explicit install in install_bundle_to_dtdb() below (v18).
 * Clearing hasBeenInited (0x0100) forces Finder to re-read on the next rebuild.
 * See IM "Using Finder Info" and the Desktop Database docs. */
static void ensure_bundle_bit(void)
{
    ProcessSerialNumber psn;
    ProcessInfoRec info;
    FSSpec me;
    FInfo fi;

    GetCurrentProcess(&psn);
    memset(&info, 0, sizeof(info));
    info.processInfoLength = sizeof(info);
    info.processName = NULL;
    info.processAppSpec = &me;
    if (GetProcessInformation(&psn, &info) != noErr) return;
    if (FSpGetFInfo(&me, &fi) != noErr) return;
    /* Normalize our OWN file's Finder info so the icon resolves:
     *   type    'APPL' (uppercase)  -- the FREF/BNDL match is case-sensitive
     *                                (a self-update FSpCreate'd lowercase
     *                                 'appl', which Finder treated as a
     *                                 document and never matched FREF 'APPL')
     *   creator 'AGCH'              -- must equal the BNDL signature
     *   kHasBundle set, kHasBeenInited clear -> Finder installs our
     *                                BNDL/FREF into the per-volume Desktop DB
     * Do it whenever ANY field is wrong so a relaunch is idempotent. */
    if (fi.fdType != (OSType)'APPL' ||
        fi.fdCreator != (OSType)APP_CREATOR ||
        (fi.fdFlags & (SInt16)0x2000) == 0 ||
        (fi.fdFlags & (SInt16)0x0100) != 0) {
        fi.fdType    = (OSType)'APPL';
        fi.fdCreator = (OSType)APP_CREATOR;
        fi.fdFlags |= (SInt16)0x2000;    /* kHasBundle      */
        fi.fdFlags &= (SInt16)~0x0100;   /* clear kHasBeenInited */
        FSpSetFInfo(&me, &fi);
    }
}

/* v18: Explicitly install our icon family + application registration into the
 * per-volume Desktop Database (DTDB).  Finder draws application/file icons from
 * the DTDB -- NOT from the file's resource fork -- so even with the bundle bit
 * set (ensure_bundle_bit) and a correct BNDL/FREF/ICN# fork on disk, the icon
 * stayed the generic "unknown application" page until we push these entries in.
 * Field sets per Inside Macintosh: More Toolbox, ch. 9 "Desktop Manager":
 *   PBDTAddIcon:  ioDTRefNum, ioTagInfo(=0), ioDTBuffer, ioDTReqCount,
 *                 ioIconType, ioFileCreator, ioFileType
 *   PBDTAddAPPL:  ioNamePtr, ioDTRefNum, ioTagInfo(=0), ioDirID, ioFileCreator
 * Uses the synchronous variants (trap 0xA260).  Must be called at launch (the
 * desktop path allocates and is not interrupt-safe), on the volume that holds the
 * app.  Idempotent: re-adding replaces the existing entries, safe every launch. */
static void install_bundle_to_dtdb(void)
{
    DTPBRec             pb;
    ProcessSerialNumber psn;
    ProcessInfoRec      info;
    FSSpec              me;
    Handle              h;
    int                 i;
    /* Each icon size we ship lives at resource id 128 under its own type; map that
     * type to the matching Desktop-Manager icon-type constant. */
    OSType  resTypes[4]   = { 'ICN#', 'icl8', 'ics#', 'ics8' };
    UInt8   iconTypes[4]  = { kLargeIcon, kLarge8BitIcon, kSmallIcon, kSmall8BitIcon };

    /* Our own FSSpec (volume ref + parent dir + name) -- same source as
     * ensure_bundle_bit() above. */
    GetCurrentProcess(&psn);
    memset(&info, 0, sizeof(info));
    info.processInfoLength = sizeof(info);
    info.processName    = NULL;
    info.processAppSpec = &me;
    if (GetProcessInformation(&psn, &info) != noErr) return;

    memset(&pb, 0, sizeof(pb));
    /* Opens the Desktop DB for the CURRENT volume (creating it if absent).
     * The VMTEST app runs from AGSHARED, so that is the DB we populate. */
    pb.ioVRefNum = me.vRefNum;
    if (PBDTGetPath(&pb) != noErr) return;

    /* Register each icon bitmap we ship against creator+type. */
    for (i = 0; i < 4; i++) {
        h = GetResource(resTypes[i], 128);
        if (h == NULL) continue;                 /* that size not present: skip */
        HLock(h);
        pb.ioDTBuffer    = *h;
        pb.ioDTReqCount  = GetHandleSize(h);
        pb.ioIconType    = iconTypes[i];
        pb.ioFileCreator = (OSType)APP_CREATOR;  /* 'AGCH' */
        pb.ioFileType    = (OSType)'APPL';
        pb.ioTagInfo     = 0;
        (void)PBDTAddIconSync(&pb);
        HUnlock(h);
    }

    /* Register the application itself: creator 'AGCH' -> this file in this dir. */
    pb.ioNamePtr     = (StringPtr)me.name;
    pb.ioFileCreator = (OSType)APP_CREATOR;
    pb.ioDirID       = me.parID;
    pb.ioTagInfo     = 0;
    (void)PBDTAddAPPLSync(&pb);

    /* Persist to the volume's Desktop DB, then release our DB reference. */
    (void)PBDTFlushSync(&pb);
    (void)PBDTCloseDown(&pb);
#ifndef mc68000
    /* OS 9 keeps Icon Services references alive across application updates.
     * Reacquire their pixels after replacing the Desktop Database family. */
    {
        IconRef iconRef = NULL;
        (void)FlushIconRefs((OSType)APP_CREATOR, 'APPL');
        if (GetIconRef(me.vRefNum, (OSType)APP_CREATOR, 'APPL', &iconRef) == noErr) {
            (void)UpdateIconRef(iconRef);
            (void)ReleaseIconRef(iconRef);
        }
    }
#endif
}

static void dialog_text(DialogPtr dlg, short number, const char *text)
{
    DialogItemType type;
    Handle item;
    Rect rect;
    Str255 p;
    long n = strlen(text);
    if (n > 255) n = 255;
    p[0] = n; memcpy(p + 1, text, n);
    GetDialogItem(dlg, number, &type, &item, &rect); SetDialogItemText(item, p);
}

static void read_dialog_text(DialogPtr dlg, short number, char *text, long size)
{
    DialogItemType type;
    Handle item;
    Rect rect;
    Str255 p;
    long n;
    GetDialogItem(dlg, number, &type, &item, &rect); GetDialogItemText(item, p);
    n = p[0]; if (n >= size) n = size - 1;
    memcpy(text, p + 1, n); text[n] = 0;
}

static Boolean ask_text(short resource, char *text, long size)
{
    DialogPtr dlg = GetNewDialog(resource, NULL, (WindowPtr)-1);
    ModalFilterUPP filter;
    short hit;
    if (!dlg) return FALSE;
    dialog_text(dlg, 1, text); SelectDialogItemText(dlg, 1, 0, 32767);
    filter = NewModalFilterUPP(update_filter);
    do { run_modal_dialog(filter, &hit); } while (hit != 2 && hit != 3);
    if (hit == 2) read_dialog_text(dlg, 1, text, size);
    DisposeModalFilterUPP(filter); DisposeDialog(dlg);
    redraw(); redraw_nav();
    return hit == 2;
}

static int dialog_port(DialogPtr dlg, short item)
{
    char text[256];
    long i, value = 0;
    read_dialog_text(dlg, item, text, sizeof text);
    if (!text[0]) return 0;
    for (i = 0; text[i]; i++) {
        if (text[i] < '0' || text[i] > '9') return 0;
        value = value * 10 + text[i] - '0';
        if (value > 65535) return 0;
    }
    return value;
}

static void model_settings(void)
{
    if (!gConnected || gBusy || gHistoryLoading) { set_status("connect before changing models"); return; }
    ModelSettingsOpen();
}

static void connection_settings(void)
{
    DialogPtr dlg;
    ModalFilterUPP filter;
    DialogItemType type;
    Handle check, discoveryCheck, findButton;
    Rect rect;
    short hit;
    int chat, update;
    char host[256], port[16];
    if (gBusy || gHistoryLoading) { SysBeep(3); return; }
    dlg = GetNewDialog(135, NULL, (WindowPtr)-1);
    if (!dlg) return;
    SetDialogDefaultItem(dlg, 2);
    SetDialogCancelItem(dlg, 3);
    dialog_text(dlg, 1, gPrefs.host);
    snprintf(port, sizeof port, "%u", gPrefs.chatPort); dialog_text(dlg, 5, port);
    snprintf(port, sizeof port, "%u", gPrefs.updatePort); dialog_text(dlg, 7, port);
    GetDialogItem(dlg, 9, &type, &check, &rect);
    SetControlValue((ControlHandle)check, (gPrefs.startup & PREF_STARTUP) != 0);
    GetDialogItem(dlg, 15, &type, &discoveryCheck, &rect);
    SetControlValue((ControlHandle)discoveryCheck, !(gPrefs.startup & PREF_DISCOVERY_OFF));
    GetDialogItem(dlg, 16, &type, &findButton, &rect);
    HiliteControl((ControlHandle)findButton, GetControlValue((ControlHandle)discoveryCheck) ? 0 : 255);
    SelectDialogItemText(dlg, 1, 0, 32767);
    filter = NewModalFilterUPP(update_filter);
    while (1) {
        run_modal_dialog(filter, &hit);
        if (hit == 3) break;
        if (hit == 14) break;
        if (hit == 9) SetControlValue((ControlHandle)check, !GetControlValue((ControlHandle)check));
        if (hit == 15) {
            SetControlValue((ControlHandle)discoveryCheck, !GetControlValue((ControlHandle)discoveryCheck));
            HiliteControl((ControlHandle)findButton, GetControlValue((ControlHandle)discoveryCheck) ? 0 : 255);
        }
        if (hit == 11) {
            dialog_text(dlg, 1, AGENT_HOST);
            snprintf(port, sizeof port, "%u", AGENT_PORT); dialog_text(dlg, 5, port);
            snprintf(port, sizeof port, "%u", HTTP_PORT); dialog_text(dlg, 7, port);
            SetControlValue((ControlHandle)check, 1);
            SetControlValue((ControlHandle)discoveryCheck, 1);
            HiliteControl((ControlHandle)findButton, 0);
        }
        if (hit != 2 && hit != 16) continue;
        read_dialog_text(dlg, 1, host, sizeof host);
        chat = dialog_port(dlg, 5); update = dialog_port(dlg, 7);
        if (hit == 2 && ((host[0] && !valid_address(host)) || !chat || !update)) {
            dialog_text(dlg, 10, "Use IPv4 and ports 1-65535; blank finds relay."); SysBeep(3); continue;
        }
        if (hit == 2) select_relay(host, (UInt16)chat, (UInt16)update);
        gPrefs.startup = (GetControlValue((ControlHandle)check) ? PREF_STARTUP : 0) |
            (GetControlValue((ControlHandle)discoveryCheck) ? 0 : PREF_DISCOVERY_OFF);
        save_preferences();
        break;
    }
    DisposeModalFilterUPP(filter); DisposeDialog(dlg);
    if (hit == 2 || hit == 16) { gDiscoverNext = hit == 16; nav_reconnect(); }
    redraw(); redraw_nav();
    if (hit == 14) model_settings();
}

static void rename_conversation(void)
{
    char text[128], command[132];
    short i = chat_index(gActiveID, FALSE);
    if (!gConnected || gBusy || gHistoryLoading || i < 0) { SysBeep(3); return; }
    strcpy(text, gChats[i].title);
    if (ask_text(132, text, sizeof text) && text[0]) {
        snprintf(command, sizeof command, "R:%s", text); send_line(command);
    }
}

static pascal Boolean delete_filter(DialogPtr dlg, EventRecord *ev, short *hit)
{
    if (ev->what == keyDown || ev->what == autoKey) {
        short ch = ev->message & charCodeMask;
        if (ch == '\r' || ch == '\n' || ch == 27 ||
            ((ev->modifiers & cmdKey) && ch == '.')) {
            *hit = 3; return TRUE; /* Return and Escape both keep the conversation. */
        }
    }
    return update_filter(dlg, ev, hit);
}

static void delete_conversation(void)
{
    DialogPtr dlg;
    ModalFilterUPP filter;
    short hit, i = chat_index(gActiveID, FALSE);
    char id[96], question[256], command[100];
    if (!gConnected || gBusy || gHistoryLoading || i < 0) return;
    strcpy(id, gActiveID);
    snprintf(question, sizeof question, "Delete the conversation \"%s\"?", gChats[i].title);
    dlg = GetNewDialog(136, NULL, (WindowPtr)-1);
    if (!dlg) return;
    dialog_text(dlg, 1, question);
    SetDialogDefaultItem(dlg, 3); SetDialogCancelItem(dlg, 3);
    filter = NewModalFilterUPP(delete_filter);
    if (!filter) { DisposeDialog(dlg); return; }
    do { run_modal_dialog(filter, &hit); } while (hit != 2 && hit != 3);
    DisposeModalFilterUPP(filter); DisposeDialog(dlg);
    if (hit == 2 && gConnected && !gBusy && !gHistoryLoading && strcmp(id, gActiveID) == 0) {
        cache_draft(); gDraftCached = TRUE; gHistoryLoading = TRUE;
        set_status("deleting conversation...");
        snprintf(command, sizeof command, "Z:%s", id); send_line(command);
    }
    redraw(); redraw_nav();
}

static Boolean contains_text(const char *text, const char *query)
{
    const char *a, *b;
    if (!query[0]) return FALSE;
    for (; *text; text++) {
        for (a = text, b = query; *a && *b; a++, b++) {
            char x = *a, y = *b;
            if (x >= 'A' && x <= 'Z') x += 'a' - 'A';
            if (y >= 'A' && y <= 'Z') y += 'a' - 'A';
            if (x != y) break;
        }
        if (!*b) return TRUE;
    }
    return FALSE;
}

static void find_transcript(Boolean ask)
{
    long n = gLineCount < MAXLINES ? gLineCount : MAXLINES;
    long base = gLineCount - n, start, step, row, top = 0;
    if (ask && !ask_text(133, gFindText, sizeof gFindText)) return;
    if (!n || !gFindText[0]) { SysBeep(3); return; }
    start = gFindRow >= base && gFindRow < gLineCount ? gFindRow + 1 : base;
    for (step = 0; step < n; step++) {
        row = base + ((start - base + step) % n);
        if (!contains_text(gLines[row % MAXLINES], gFindText)) continue;
        clear_text_selection(); gFindRow = row; gSelectedMessage = row;
        gComposerFocus = FALSE;
        if (gTE) { SetPort(gWin); TEDeactivate(gTE); }
        while (gSelectedMessage > base && !(gLineMeta[gSelectedMessage % MAXLINES] & 4)) gSelectedMessage--;
        for (long j = base; j < row; j++) top += gLineH[j % MAXLINES];
        gFollow = FALSE;
        set_scroll(gDocHeight - tx_viewh() - top);
        set_status("match found; Command-G finds next");
        redraw(); return;
    }
    set_status("no matching text"); SysBeep(3);
}

static const char *copy_row_text(long idx)
{
    if (gLineMeta[idx] & 16) return "[Image]";
    if ((gLineMeta[idx] & 4) && !gLines[idx][0]) {
        if ((gLineMeta[idx] & 3) == 1) return "You";
        if ((gLineMeta[idx] & 3) == 2) return "Agent";
    }
    return gLines[idx];
}

static void copy_transcript(Boolean selection)
{
    long n = gLineCount < MAXLINES ? gLineCount : MAXLINES;
    long base = gLineCount - n, first = base, end = gLineCount, bytes = 0, j;
    Ptr text;
    if (selection && has_text_selection()) { copy_text_selection(); return; }
    if (selection) {
        if (gSelectedMessage < base || gSelectedMessage >= end) return;
        first = gSelectedMessage; end = first + 1;
        while (end < gLineCount && !(gLineMeta[end % MAXLINES] & 4)) end++;
    }
    for (j = first; j < end; j++) bytes += strlen(copy_row_text(j % MAXLINES)) + 1;
    text = NewPtr(bytes + 1);
    if (!text) { SysBeep(3); return; }
    bytes = 0;
    for (j = first; j < end; j++) {
        long idx = j % MAXLINES, len = strlen(copy_row_text(idx));
        memcpy(text + bytes, copy_row_text(idx), len); bytes += len;
        if ((gLineMeta[idx] & 4) || gLineBreak[idx] == 1 || gLineBreak[idx] == 3) text[bytes++] = '\r';
        else if (gLineBreak[idx] == 0) text[bytes++] = ' ';
    }
    ZeroScrap();
    if (PutScrap(bytes, 'TEXT', text) != noErr) {
        DisposePtr(text); set_status("could not copy text"); SysBeep(3); return;
    }
    gSystemScrapOwned = TRUE; DisposePtr(text);
    set_status(selection ? "message copied" : "transcript copied");
}

static void remote_state(RemoteMacDiagnostics *d)
{
    long row, start = gLineCount > MAXLINES ? gLineCount - MAXLINES : 0;
    d->version = AGENTCHAT_VERSION; d->status = gStatus;
    d->model = gModelID; d->conversation = gActiveID;
    d->connected = gConnected; d->busy = gBusy; d->loading = gHistoryLoading;
#ifdef AGENTCHAT_DEV_FIXTURE
    d->testBuild = TRUE;
#else
    d->testBuild = FALSE;
#endif
    d->transcriptRows = gLineCount;
    d->draftBytes = gTE ? (*gTE)->teLength : 0; d->queueBytes = gQLen - gQPos;
    d->draftImage = gDraftImage; d->imageUploading = ChatImagesUploading();
    d->imageReady = gDraftImage[0] && ChatImagesReady(gDraftImage);
    d->modelImages = gModelSupportsImages;
    d->conversationTop = gConversationTop; d->modelRows = gModelRows;
    d->questionID = ToolQuestionsID(); d->questionIndex = ToolQuestionsIndex();
    d->questionCustomBytes = ToolQuestionsCustomBytes(); d->questionEditBytes = ToolQuestionsEditBytes();
    d->imageRows = 0;
    d->transcriptBuffered = gTranscriptWorld != NULL;
    d->transcriptFrames = gTranscriptFrames; d->windowFrames = gWindowFrames;
    for (row = start; row < gLineCount; row++) if (gLineMeta[row % MAXLINES] & 16) d->imageRows++;
}

static OSErr remote_action(const char *action, const char *value)
{
    long i, n = strlen(value);
    if (!strcmp(action, "redraw")) { redraw(); redraw_nav(); return noErr; }
    if (!strcmp(action, "arrange_windows")) { arrange_windows(); return noErr; }
    if (!strcmp(action, "stop")) {
        if (gBusy && gConnected) send_line("A:");
        return noErr;
    }
    if (gBusy || gHistoryLoading) return fBsyErr;
#ifdef AGENTCHAT_DEV_FIXTURE
    if (!strcmp(action, "image_test")) return ChatImagesTestScrap();
    if (!strcmp(action, "image_test_restore")) return ChatImagesRestoreScrap();
#endif
    if (ChatImagesUploading() && (!strcmp(action, "new_chat") || !strcmp(action, "select_model"))) return fBsyErr;
    if (!strcmp(action, "new_chat")) { if (!gConnected) return paramErr; request_conversation(""); return noErr; }
    if (!strcmp(action, "select_model")) { if (model_index(value, FALSE) < 0) return paramErr; request_model(value); return noErr; }
    if (!strcmp(action, "set_draft")) {
        if (!gTE || n > DRAFT_LIMIT) return paramErr;
        for (i = 0; i < n; i++) if (value[i] != '\r' && (value[i] < ' ' || value[i] > '~')) return paramErr;
        SetPort(gWin); TESetText(value, n, gTE); TESetSelect(n, n, gTE); TESelView(gTE);
        cache_draft(); gDirty = TRUE; return noErr;
    }
    return paramErr;
}

/* AESend may yield while the port handles a private command. Service only
 * the housekeeping events it supplies; transport/RPC dispatch must wait for
 * the outer event loop to avoid reentering the current native request. */
static Boolean application_command_idle(const EventRecord *event, void *context)
{
    GrafPtr saved;
    EventRecord ev = *event;
    Boolean consumed;
    observe_background_event(&ev);
    (void)context;
    GetPort(&saved);
    consumed = ToolQuestionsEvent(&ev);
    if (!consumed) consumed = ModelSettingsEvent(&ev);
    if (!consumed) {
        switch (ev.what) {
            case nullEvent:
                paint_changes();
                if (gNavVisible && gNavDirty) redraw_nav();
                animate_status();
                if (gTE && FrontWindow() == gWin && gComposerFocus && !gBackground) {
                    SetPort(gWin); TEIdle(gTE);
                }
                break;
            case updateEvt:
                if ((WindowPtr)ev.message == gNavWin && gNavVisible) {
                    SetPort(gNavWin); BeginUpdate(gNavWin);
                    draw_nav(); EndUpdate(gNavWin);
                } else if ((WindowPtr)ev.message == gWin) {
                    SetPort(gWin); BeginUpdate(gWin);
                    redraw(); EndUpdate(gWin);
                }
                break;
            case activateEvt:
                gTextDragging = FALSE;
                if ((WindowPtr)ev.message == gWin && gTE) {
                    SetPort(gWin);
                    if ((ev.modifiers & activeFlag) && gComposerFocus) TEActivate(gTE);
                    else TEDeactivate(gTE);
                    gDirty = TRUE;
                }
                gNavDirty = TRUE;
                break;
            case osEvt:
                gTextDragging = FALSE;
                if ((ev.message >> 24) == suspendResumeMessage && gWin) {
                    SetPort(gWin);
                    if (ev.message & resumeFlag) {
                        TEFromScrap(); gSystemScrapOwned = TRUE;
                        if (gTE && FrontWindow() == gWin && gComposerFocus) TEActivate(gTE);
                    } else {
                        if (gTE) TEDeactivate(gTE);
                        if (!gSystemScrapOwned) { ZeroScrap(); TEToScrap(); }
                    }
                    gDirty = gNavDirty = TRUE;
                }
                break;
        }
    }
    SetPort(saved);
    return FALSE;
}

int main(void)
{
    RemoteMacInit(q_put, remote_state, remote_action);
    RemoteMacSetApplicationIdle(application_command_idle, NULL);
    ChatImagesInit(send_line, image_notice);
    ModelSettingsInit(send_line);
    ToolQuestionsInit(send_line);
    load_preferences();
    gAutoConnect = (gPrefs.startup & PREF_STARTUP) != 0;
    /* An offline startup still belongs to the bookmarked conversation. Cache
     * drafts there so connecting later cannot replace typed text with an empty
     * editor while restoring its history. */
    strncpy(gActiveID, gPrefs.lastID, sizeof gActiveID - 1);
    chat_index(gActiveID, TRUE);
    EventRecord ev;
    short ch;
    long sel;
    OSErr quitEventError;

    InitGraf(&qd.thePort);
    InitFonts();
    InitWindows();
    InitMenus();
    TEInit();
    InitDialogs(NULL);
    quitEventError = install_quit_event();
    RegisterAppearanceClient();
    ensure_bundle_bit();        /* set our own file's bundle bit / type / creator */
    install_bundle_to_dtdb();   /* v18: push icon family + app reg into Desktop DB */
    build_menu();

    {
        /* Two-window composition (plan §2): nav window on the left, chat
         * window on the right, both laid out inside the real desktop bounds
         * (below the menu bar, with side margins).  Chat is created last so
         * it is the front/key window (the composer owns the keyboard). */
        Rect sb = qd.screenBits.bounds;
        long mb = GetMBarHeight();
        long total, margin, top, navL, chatL;
        Rect navR, chatR;

        if (mb < 20) mb = 24;             /* metrics not ready — assume std bar */
        total  = (long)NAV_W + 12 + WIN_W;
        margin = (long)sb.right - total;
        if (margin < 8) margin = 8;
        margin /= 2;
        top = mb + 40; if (top < 48) top = 48;
        if (top + WIN_H > sb.bottom - 8) top = (long)(sb.bottom - 8) - WIN_H;
        navL  = margin;
        chatL = margin + NAV_W + 12;
        navR.top = (short)top; navR.left = (short)navL;
        navR.bottom = (short)(top + NAV_H); navR.right = (short)(navL + NAV_W);
        chatR.top = (short)top; chatR.left = (short)chatL;
        chatR.bottom = (short)(top + WIN_H); chatR.right = (short)(chatL + WIN_W);

        /* Native document chrome, including tracked close/zoom/grow boxes. */
        gNavWin = NewCWindow(NULL, &navR, (StringPtr)"\pAgentChat", TRUE,
                           zoomDocProc, (WindowPtr)-1, TRUE, 0);
        gWin    = NewCWindow(NULL, &chatR, (StringPtr)"\pChat", TRUE,
                           zoomDocProc, (WindowPtr)-1, TRUE, 0);
    }
    SetPort(gWin);
    if (!gWin || !gNavWin) quit_app();
    InitCursor();
    init_metrics();    /* resolve real font line metrics before any layout */
    layout_chat();
#ifdef AGENTCHAT_DEV_FIXTURE
    gBootTrace = FALSE;
    /* Load the large QA fixture explicitly with Command-T. */
#endif

    {
        /* Mac OS 8-9 Appearance scrollbar: native proportional thumb and
         * live tracking. The action callback repaints while still held. */
        Rect sb = { TX_Y, SB_L, TX_B, SB_R };
        gScrollBar = NewControl(gWin, &sb, "\p", TRUE, 0, 0, 0,
                               kControlScrollBarLiveProc, 0);
    }

    /* Send button: a REAL native push button (ControlDefProc 0 = pushButProc),
     * not a drawn rect. value=0, min=0, max=1, refCon=0. The CDEF draws the
     * "Send" title and the rounded chrome; DrawControls renders it and
     * TrackControl (in the mouseDown handler) inverts on press and returns
     * non-zero only when the user releases inside — the OS owns the press, so
     * there is no hand-rolled state and no lost-release wedge. */
    gSendBtn = NewControl(gWin, &gBtnRect, "\pSend", TRUE, 0, 0, 1, 0, 0);
    {
        Rect remove = {IN_Y + 44, IN_X + 5, IN_Y + 61, IN_X + 88};
        gRemoveImageBtn = NewControl(gWin, &remove, "\pRemove", FALSE, 0, 0, 1, pushButProc, 0);
    }
    {
        Rect stop = { IN_Y + 8, BTN_L, IN_Y + 30, BTN_R };
        gStopBtn = NewControl(gWin, &stop, "\pStop", TRUE, 0, 0, 1, pushButProc, 0);
    }

    /* Native TextEdit owns the draft, selection and caret. TEAutoView keeps
     * the insertion point visible when a long draft exceeds the view. */
    SetPort(gWin);
    {
        Rect tr = gInRect;
        InsetRect(&tr, 6, 4);
        gTE = TENew(&tr, &tr);
    }
    if (gTE != NULL) {
        (*gTE)->txFont = kFontIDGeneva;
        (*gTE)->txSize = 12;
        (*gTE)->txFace = (StyleField)0;
        (*gTE)->txMode = srcCopy;
        TEActivate(gTE);
    }

    if (gTE) TEAutoView(TRUE, gTE);
    gScrollAction = NewControlActionUPP(scroll_action);
    create_nav_controls();
    try_connect();
    set_status(gConnected ? "ready" : "connecting...");
    if (quitEventError != noErr) {
        char warning[128];
        snprintf(warning, sizeof warning, "** native Quit event unavailable (%d); use File > Quit", quitEventError);
        push_line(warning);
        set_status(warning + 3);
    }
    redraw();
    if (gNavWin) { SetPort(gNavWin); draw_nav(); SetPort(gWin); }
    gBootTrace = FALSE;    /* stop the raw OT boot trace from overwriting the
                            * transcript/status from here on */
    cleanup_prev();

    while (1) {
        IdleMetricsEnd();
        finish_deferred_quit();
        {
            unsigned long sleep = RemoteMacBinaryRemaining() ? 0 : (gTextDragging ? 1 : (RemoteMacBusy() ? 0 : 4));
#ifndef AGENTCHAT_IDLE_BASELINE
            /* OT's notifier calls WakeUpProcess on arrival. One-second idle
             * background polling is a fallback for missed/coalesced notices
             * and bounds disconnect/reconnect/transaction housekeeping.
             * Preserve v88's zero sleep throughout RPC/import bursts. */
            if (sleep == 4 && gBackground && !(gBusy && !EmptyRgn(gWin->visRgn))) sleep = 60;
            if (sleep > 1 && (gQLen != gQPos || RemoteMacPendingReplyBytes() || ChatImagesUploading())) sleep = 1;
#endif
            WaitNextEvent(everyEvent, &ev, sleep, NULL);
            observe_background_event(&ev);
            IdleMetricsBegin(ev.what, gBackground, sleep);
        }
        if (ev.what == mouseDown || ev.what == keyDown || ev.what == autoKey ||
            ev.what == activateEvt || ev.what == osEvt) {
            dismiss_help();
        }
        pump_ot_events();
        RemoteMacTick();
        if (gConnected) {
            poll_recv();
            q_flush();
            ChatImagesTick(q_upload_space());
        } else {
            try_connect();
        }
        /* Paint transcript/status changes while connected. Previously the
         * screen only repainted on updateEvt, so prompts, streaming replies
         * and the cleared input box never became visible (2026-09-28). */
        paint_changes();
        if (gNavVisible && gNavDirty) redraw_nav();
        animate_status();
        ModelSettingsTick(gConnected);
        ToolQuestionsTick(gConnected);
#ifndef AGENTCHAT_IDLE_BASELINE
        if (!gBackground) update_edit_menu();
#else
        update_edit_menu();
#endif

        {
            Boolean questionOpen = ToolQuestionsIsOpen();
            if (ToolQuestionsEvent(&ev)) {
                if (questionOpen && !ToolQuestionsIsOpen()) gDirty = gNavDirty = TRUE;
                continue;
            }
            Boolean wasOpen = ModelSettingsIsOpen();
            if (ModelSettingsEvent(&ev)) {
                if (wasOpen && !ModelSettingsIsOpen()) gDirty = gNavDirty = TRUE;
                continue;
            }
        }

        switch (ev.what) {
            case nullEvent:
                if (gTextDragging) {
                    Point p; SetPort(gWin); GetMouse(&p); text_drag(p);
                    if (ev.modifiers & btnState) gTextDragging = FALSE;
                }
#ifndef AGENTCHAT_IDLE_BASELINE
                if (!gBackground) idle_help(ev.where);
#else
                idle_help(ev.where);
#endif
                /* Native caret blink: TEIdle toggles and redraws the insertion
                 * point on the record's own caret schedule (no hand-rolled
                 * gBlink/gCaretVis).  Must run with the composer's port set. */
                if (gTE && FrontWindow() == gWin && gComposerFocus
#ifndef AGENTCHAT_IDLE_BASELINE
                    && !gBackground
#endif
                ) { SetPort(gWin); TEIdle(gTE); }
                break;

            case keyDown:
            case autoKey:
                ch = (short)(ev.message & charCodeMask);
                if ((ev.modifiers & cmdKey) != 0) {
                    long msel = MenuEvent(&ev); /* native Shift/Option shortcut matching */
                    if (msel != 0) do_menu(msel);
                    else if (ch == 'w' || ch == 'W') {
                        if (FrontWindow() == gNavWin) {
                            HideWindow(gNavWin); gNavVisible = FALSE; SelectWindow(gWin);
                        } else quit_app();
                    }
                    break;
                }
                if (FrontWindow() != gWin) break;
                if (ch == 11 || ch == 12 || ch == 30 || ch == 31) {
                    set_scroll(gScroll + ((ch == 11 || ch == 30) ? 1 : -1) *
                               ((ch == 11 || ch == 12) ? tx_viewh() - 24 : 24));
                    redraw(); break;
                }
                if (!gComposerFocus || !gTE) break;
                SetPort(gWin);
                if (ch == '\r' || ch == '\n') {
                    if (ev.modifiers & shiftKey) {
                        if ((*gTE)->teLength < DRAFT_LIMIT) {
                            TEDelete(gTE); TEInsert("\r", 1, gTE); TESelView(gTE);
                            gDirty = TRUE;
                        }
                    } else if (ev.what == keyDown) send_prompt();
                } else if (ch == 28 || ch == 29) {
                    long pos = ch == 28 ? (*gTE)->selStart - 1 : (*gTE)->selEnd + 1;
                    if (pos < 0) pos = 0;
                    if (pos > (*gTE)->teLength) pos = (*gTE)->teLength;
                    TESetSelect(pos, pos, gTE); TESelView(gTE);
                } else if ((ev.modifiers & 0x0800) != 0) {
                    /* Keep the Option/high-bit sanitiser: never hand TEKey
                     * arbitrary event bytes. Trusted CR uses TEInsert above. */
                } else if ((ch >= ' ' && ch <= '~') || ch == '\b' || ch == 0x7F) {
                    if (ch == '\b' || ch == 0x7F ||
                        (*gTE)->teLength - ((*gTE)->selEnd - (*gTE)->selStart) < DRAFT_LIMIT) {
                        TEKey((CharParameter)ch, gTE); TESelView(gTE); gDirty = TRUE;
                    } else SysBeep(3);
                }
                break;

            case activateEvt:
                gTextDragging = FALSE;
                if ((WindowPtr)ev.message == gWin && gTE) {
                    SetPort(gWin);
                    if ((ev.modifiers & activeFlag) && gComposerFocus) TEActivate(gTE);
                    else TEDeactivate(gTE);
                    gDirty = TRUE;
                }
                gNavDirty = TRUE;
                break;

            case osEvt:
                gTextDragging = FALSE;
                if ((ev.message >> 24) == suspendResumeMessage) {
                    gBackground = !(ev.message & resumeFlag);
                    SetPort(gWin);
                    if (ev.message & resumeFlag) {
                        TEFromScrap();
                        gSystemScrapOwned = TRUE;
                        if (gTE && FrontWindow() == gWin && gComposerFocus) TEActivate(gTE);
                    } else {
                        if (gTE) TEDeactivate(gTE);
                        if (!gSystemScrapOwned) { ZeroScrap(); TEToScrap(); }
                    }
                    gDirty = gNavDirty = TRUE;
                }
                break;

            case mouseDown:
                /* Two-window routing: find the owning window + part in global
                 * coords FIRST, then convert to that window's local coords
                 * before any rect/control hit-test. */
                {
                    WindowPtr fw = NULL;
                    short part = FindWindow(ev.where, &fw);
                    if (ev.where.v <= GetMBarHeight()) {
                        sel = MenuSelect(ev.where);
                        do_menu(sel);
                        DrawMenuBar();
                        break;
                    }
                    if (fw == NULL) break;
                    /* Make the clicked window the active one so the Window
                     * Manager draws its title bar / close box in the active
                     * state (the old single-window app never needed this). */
                    if (fw != FrontWindow()) SelectWindow(fw);
                    SetPort(fw);
                    {
                        Point pt = ev.where;
                        GlobalToLocal(&pt);
                        if (fw == gNavWin) {
                            if (part == inDrag) {
                                DragWindow(gNavWin, ev.where, &qd.screenBits.bounds);
                                keep_window_visible(gNavWin);
                                draw_nav();
                            } else if (part == inCollapseBox) {
                                if (TrackBox(gNavWin, ev.where, part)) CollapseWindow(gNavWin, !IsWindowCollapsed(gNavWin));
                            } else if (part == inGoAway) {
                                /* Closing nav hides it; the chat keeps running
                                 * and Window>Show Navigation reopens it (§28). */
                                if (TrackGoAway(gNavWin, ev.where)) {
                                    HideWindow(gNavWin); gNavVisible = FALSE; SelectWindow(gWin);
                                }
                            } else if (part == inGrow) {
                                Rect limits = {300, 180, 560, 320};
                                long size = grow_in_place(gNavWin, ev.where, &limits);
                                if (size) {
                                    gNavW = (short)(size & 0xFFFF); gNavH = (short)((unsigned long)size >> 16);
                                    layout_nav(); redraw_nav();
                                }
                            } else if (part == inZoomIn || part == inZoomOut) {
                                if (TrackBox(gNavWin, ev.where, part)) {
                                    ZoomWindow(gNavWin, part, TRUE);
                                    keep_window_visible(gNavWin);
                                    gNavW = gNavWin->portRect.right; gNavH = gNavWin->portRect.bottom;
                                    layout_nav(); redraw_nav();
                                }
                            } else if (part == inContent) {
                                ControlHandle ctl = NULL;
                                FindControl(pt, gNavWin, &ctl);
                                if (ctl == gReconnectBtn || ctl == gClearBtn || ctl == gSettingsBtn) {
                                    if (!gBusy && !gHistoryLoading && TrackControl(ctl, pt, NULL)) {
                                        if (ctl == gReconnectBtn) nav_reconnect();
                                        else if (ctl == gSettingsBtn) connection_settings();
                                        else request_conversation("");
                                    }
                                } else if (gModelList &&
                                           (PtInRect(pt, &(**gModelList).rView) || ctl == (**gModelList).vScroll)) {
                                    if (!gBusy && !gHistoryLoading && !gModelBuilding) {
                                        Point cell = {0, 0};
                                        LClick(pt, ev.modifiers, gModelList);
                                        if (LGetSelect(TRUE, &cell, gModelList) && cell.v >= 0 && cell.v < gModelRows)
                                            request_model(gModels[gModelMap[cell.v]].id);
                                    }
                                } else if (gNavList && !gBusy && !gHistoryLoading && !gListBuilding) {
                                    Point cell = {0, 0};
                                    LClick(pt, ev.modifiers, gNavList);
                                    if (LGetSelect(TRUE, &cell, gNavList) && cell.v >= 0 && cell.v < gNavRows) {
                                        const char *id = cell.v ? gChats[gNavMap[cell.v]].id : "";
                                        if (strcmp(id, gActiveID) != 0) request_conversation(id);
                                    }
                                }
                            }
                            /* The List Manager owns conversation selection. */
                        } else {                       /* chat window */
                            ControlHandle ctl = NULL;
                            if (part == inDrag) {
                                DragWindow(gWin, ev.where, &qd.screenBits.bounds);
                                keep_window_visible(gWin);
                            } else if (part == inCollapseBox) {
                                if (TrackBox(gWin, ev.where, part)) CollapseWindow(gWin, !IsWindowCollapsed(gWin));
                                redraw();
                            } else if (part == inGoAway) {
                                if (TrackGoAway(gWin, ev.where)) quit_app();
                            } else if (part == inGrow || part == inZoomIn || part == inZoomOut) {
                                long oldh = tx_viewh(), size = 0;
                                Rect limits = {300, 420, 560, 792};
                                if (part == inGrow) size = grow_in_place(gWin, ev.where, &limits);
                                else if (TrackBox(gWin, ev.where, part)) {
                                    ZoomWindow(gWin, part, TRUE);
                                    keep_window_visible(gWin);
                                    size = ((long)gWin->portRect.bottom << 16) | gWin->portRect.right;
                                }
                                if (size) {
                                    gChatW = (short)(size & 0xFFFF); gChatH = (short)((unsigned long)size >> 16);
                                    layout_chat(); reflow_transcript(oldh); redraw();
                                }
                            } else if (part == inContent) {
                                (void)FindControl(pt, gWin, &ctl);
                                if (ctl == gScrollBar && gScrollBar != NULL) {
                                    TrackControl(gScrollBar, pt, gScrollAction);
                                    set_scroll(ctl_to_scroll(GetControlValue(gScrollBar)));
                                    redraw();
                                } else if (ctl == gStopBtn && gConnected && gBusy) {
                                    if (TrackControl(ctl, pt, NULL)) send_line("A:");
                                } else if (ctl == gSendBtn && gSendBtn != NULL) {
                                    /* Native push button (pushButProc). The
                                     * Control Manager's TrackControl owns the
                                     * whole press: it inverts the button while
                                     * held (HiliteControl) and its OWN tracking
                                     * loop reads the release, so a dropped/
                                     * late mouseUp can't wedge us the way a
                                     * hand-rolled Button() spin did.  It
                                     * returns non-zero only on release-inside.
                                     * Guard mirrors the disabled (dimmed) look:
                                     * a dimmed Send does nothing. */
                                    if (gConnected && (!gBusy || ToolQuestionsPending())) {
                                        if (TrackControl(gSendBtn, pt,
                                                        (ControlActionUPP)0) != 0)
                                            send_prompt();
                                    }
                                } else if (ctl == gRemoveImageBtn && gDraftImage[0] && !gBusy && !gHistoryLoading) {
                                    if (TrackControl(ctl, pt, NULL)) {
                                        ChatImagesCancel(gDraftImage); gDraftImage[0] = 0;
                                        cache_draft(); layout_chat(); gDirty = TRUE;
                                    }
                                } else if (PtInRect(pt, &gInRect)) {
                                    /* Native caret/selection (v29): TEClick
                                     * places the insertion point on a click or
                                     * drag-selects (Shift extends) in its own
                                     * tracking loop, then activates the record.
                                     * No hand-drawn caret bookkeeping. */
                                    if (gTE != NULL) {
                                        SetPort(gWin);
                                        gComposerFocus = TRUE;
                                        TEActivate(gTE);
                                        TEClick(pt,
                                                (Boolean)((ev.modifiers & shiftKey) != 0),
                                                gTE);
                                        gDirty = TRUE;
                                    }
                                } else if (PtInRect(pt, &gTxRect)) {
                                    gComposerFocus = FALSE;
                                    if (gTE) TEDeactivate(gTE);
                                    {
                                        TranscriptPosition hit = text_hit(pt);
                                        if (!(ev.modifiers & shiftKey) || gTextAnchor.row < 0) gTextAnchor = hit;
                                        gTextFocus = hit; gTextDragging = hit.row >= 0; gFollow = FALSE;
                                        gSelectedMessage = -1; gDirty = TRUE;
                                        /* Header clicks retain the convenient whole-message Copy. */
                                        if (hit.row >= 0 && (gLineMeta[hit.row % MAXLINES] & 4)) gSelectedMessage = hit.row;
                                    }
                                }
                            }
                        }
                        SetPort(gWin);
                    }
                }
                break;

            case mouseUp:
                if (gTextDragging) { Point p = ev.where; SetPort(gWin); GlobalToLocal(&p); text_drag(p); gTextDragging = FALSE; }
                /* No manual button state to unwind: the native Send button's
                 * press/release is fully handled inside TrackControl above, and
                 * the Control Manager already restored its resting look. */
                break;

            case kHighLevelEvent:
                AEProcessAppleEvent(&ev);
                break;

            case updateEvt:
                {
                    WindowPtr wp = (WindowPtr)ev.message;
                    if (wp == gNavWin && gNavVisible) {
                        /* BeginUpdate does NOT change the current port — without
                         * the SetPort the whole nav painted into whatever port was
                         * current (usually gWin) => the +222px nav ghost that
                         * the one-shot clear can never erase. */
                        SetPort(gNavWin);
                        BeginUpdate(gNavWin);
                        draw_nav();
                        EndUpdate(gNavWin);
                        SetPort(gWin);
                    } else if (wp == gWin) {
                        SetPort(gWin);
                        BeginUpdate(gWin);
                        redraw();
                        EndUpdate(gWin);
                    }
                }
                break;

            default:
                break;
        }
    }
    return 0;
}

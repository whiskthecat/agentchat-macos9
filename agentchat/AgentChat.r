/*
 * AgentChat.r — Mac OS 9 resources (compiled by Retro68's Rez).
 */

#include "Types.r"
#include "ControlDefinitions.r"
#include "AgentChatHelp.r"

/* Update prompt dialog: id 130.
   DITL items are numbered in LISTED order (1 = first).  Layout below is
   dialog-relative and fully inside the 120x380 box.  The C code relies on
   THIS numbering: 1 = EditText, 2 = OK (default), 3 = Cancel, 4 = StaticText.
   (Old build listed OK first with out-of-range rects -> invisible Cancel,
   mis-read text item, and a "blank" dialog.  Fixed 2026-09-28.) */
resource 'DLOG' (130) {
    { 80, 110, 80 + 120, 110 + 380 },
    dBoxProc,
    visible,
    noGoAway,
    0,
    130,
    "Check for Update",
    centerMainScreen
};

resource 'DITL' (130) {
    {
        { 30, 10, 50, 370 },
        EditText { enabled, "AgentChat.NEW" };

        { 75, 190, 95, 280 },
        Button { enabled, "OK" };

        { 75, 285, 95, 375 },
        Button { enabled, "Cancel" };

        { 8, 10, 24, 370 },
        StaticText { enabled, "Update file to fetch from the build server:" };
        { 0, 0, 0, 0 }, HelpItem { disabled, HMScanhdlg { 130 } };
    }
};

#include "Processes.r"

resource 'DLOG' (132) {
    { 100, 160, 230, 540 }, dBoxProc, visible, noGoAway, 0, 132,
    "Rename Conversation", centerMainScreen
};
resource 'DITL' (132) { {
    { 36, 12, 58, 368 }, EditText { enabled, "" };
    { 86, 180, 110, 268 }, Button { enabled, "Rename" };
    { 86, 280, 110, 368 }, Button { enabled, "Cancel" };
    { 10, 12, 28, 368 }, StaticText { enabled, "Conversation name:" };
    { 0, 0, 0, 0 }, HelpItem { disabled, HMScanhdlg { 132 } };
} };

resource 'DLOG' (133) {
    { 100, 160, 230, 540 }, dBoxProc, visible, noGoAway, 0, 133,
    "Find in Transcript", centerMainScreen
};
resource 'DITL' (133) { {
    { 36, 12, 58, 368 }, EditText { enabled, "" };
    { 86, 180, 110, 268 }, Button { enabled, "Find Next" };
    { 86, 280, 110, 368 }, Button { enabled, "Cancel" };
    { 10, 12, 28, 368 }, StaticText { enabled, "Find text (Command-G repeats):" };
    { 0, 0, 0, 0 }, HelpItem { disabled, HMScanhdlg { 133 } };
} };

resource 'DLOG' (135) {
    { 70, 160, 386, 560 }, noGrowDocProc, visible, goAway, 0, 135,
    "Connection Settings", centerMainScreen
};
/* Appearance owns the Platinum background, edit frames and movable modal
 * tracking, just as in the system's control panels. */
resource 'dlgx' (135) {
    versionZero {
    kDialogFlagsUseThemeBackground | kDialogFlagsUseThemeControls |
    kDialogFlagsHandleMovableModal
    }
};
resource 'CNTL' (1350) {
    { 16, 14, 154, 386 }, 0, visible, 0, 0,
    kControlGroupBoxTextTitleProc, 0, "Bridge"
};
resource 'CNTL' (1351) {
    { 164, 14, 215, 386 }, 0, visible, 0, 0,
    kControlGroupBoxTextTitleProc, 0, "Startup"
};
resource 'DITL' (135) { {
    { 44, 176, 62, 372 }, EditText { enabled, "" };
    { 284, 294, 304, 386 }, Button { enabled, "Connect" };
    { 284, 190, 304, 282 }, Button { enabled, "Cancel" };
    { 44, 30, 62, 166 }, StaticText { enabled, "Bridge address:" };
    { 80, 176, 98, 250 }, EditText { enabled, "" };
    { 80, 30, 98, 166 }, StaticText { enabled, "Chat port:" };
    { 112, 176, 130, 250 }, EditText { enabled, "" };
    { 112, 30, 130, 166 }, StaticText { enabled, "Update HTTP port:" };
    { 185, 30, 203, 370 }, CheckBox { enabled, "Connect when AgentChat starts" };
    { 260, 14, 278, 386 }, StaticText { enabled, "Blank address: find relay on the local network." };
    { 284, 14, 304, 100 }, Button { enabled, "Defaults" };
    { 16, 14, 154, 386 }, Control { enabled, 1350 };
    { 164, 14, 215, 386 }, Control { enabled, 1351 };
    { 284, 108, 304, 182 }, Button { enabled, "Models..." };
    { 230, 30, 248, 270 }, CheckBox { enabled, "Find relay if address fails" };
    { 228, 276, 250, 372 }, Button { enabled, "Find Relay" };
    { 0, 0, 0, 0 }, HelpItem { disabled, HMScanhdlg { 135 } };
} };

resource 'DLOG' (136) {
    { 90, 160, 280, 540 }, dBoxProc, visible, noGoAway, 0, 136,
    "Delete Conversation", centerMainScreen
};
resource 'DITL' (136) { {
    { 14, 16, 68, 364 }, StaticText { enabled, "Delete this conversation?" };
    { 146, 280, 172, 368 }, Button { enabled, "Delete" };
    { 146, 180, 172, 268 }, Button { enabled, "Cancel" };
    { 78, 16, 126, 364 }, StaticText { enabled, "This permanently deletes its messages from the bridge. This cannot be undone." };
    { 0, 0, 0, 0 }, HelpItem { disabled, HMScanhdlg { 136 } };
} };

resource 'SIZE' (-1) {
    reserved,
    acceptSuspendResumeEvents,
    reserved,
    canBackground,
    doesActivateOnFGSwitch,
    backgroundAndForeground,
    dontGetFrontClicks,
    ignoreChildDiedEvents,
    is32BitCompatible,
    isHighLevelEventAware,
    onlyLocalHLEvents,
    notStationeryAware,
    dontUseTextEditServices,
    reserved,
    reserved,
    reserved,
    16 * 1024 * 1024,  /* Native clipboard pictures, previews and color snapshots. */
    8 * 1024 * 1024
};

/* Finder + About-box artwork (generated pixels — see mkicon.py). */
#include "icon.r"
#include "ui_icons.r"

/* About box: id 131. DITL 1 = OK (the C loop waits for itemHit == 1). */
resource 'DLOG' (131) {
    { 90, 130, 90 + 150, 130 + 300 },
    dBoxProc,
    visible,
    noGoAway,
    0,
    131,
    "About AgentChat",
    centerMainScreen
};

resource 'DITL' (131) {
    {
        { 100, 120, 124, 180 },
        Button { enabled, "OK" };

        { 12, 60, 44, 92 },
        Icon { enabled, 128 };

        { 12, 100, 30, 290 },
        StaticText { enabled, "AgentChat 1.0" };

        { 32, 100, 48, 290 },
        StaticText { enabled, "Chat terminal for coding agents" };

        { 50, 100, 66, 290 },
        StaticText { enabled, "on your iMac G3." };

        { 70, 60, 86, 290 },
        StaticText { enabled, "Type a prompt, press Return." };
        { 0, 0, 0, 0 }, HelpItem { disabled, HMScanhdlg { 131 } };
    }
};

resource 'DLOG' (137) {
    { 70, 128, 490, 672 }, zoomDocProc, visible, goAway, 0, 137,
    "Models", centerMainScreen
};
resource 'dlgx' (137) { versionZero {
    kDialogFlagsUseThemeBackground | kDialogFlagsUseThemeControls
} };
resource 'DITL' (137) { {
    { 72, 16, 314, 512 }, UserItem { enabled };
    { 384, 442, 404, 528 }, Button { enabled, "Done" };
    { 384, 16, 404, 118 }, Button { enabled, "Automatic" };
    { 384, 128, 404, 214 }, Button { enabled, "Refresh" };
    { 12, 16, 42, 528 }, StaticText { enabled, "Choose the models shown in your sidebar. Connected providers are discovered automatically." };
    { 334, 16, 351, 528 }, StaticText { enabled, "" };
    { 356, 16, 374, 528 }, StaticText { enabled, "" };
    { 318, 16, 332, 528 }, StaticText { enabled, "Loading connected providers..." };
    { 0, 0, 0, 0 }, HelpItem { disabled, HMScanhdlg { 137 } };
} };

/* Dialog Manager owns these text styles; buttons keep the system font. */
#define MODEL_TEXT_STYLE dataItem { kDialogFontUseFontMask | kDialogFontUseFaceMask | kDialogFontUseSizeMask, 3, 10, 0, 0, 0, 0, 0, 0, 0, 0, 0, "" }
resource 'dftb' (137) { versionZero { {
    skipItem {}; skipItem {}; skipItem {}; skipItem {};
    MODEL_TEXT_STYLE; MODEL_TEXT_STYLE; MODEL_TEXT_STYLE; MODEL_TEXT_STYLE;
    skipItem {};
} } };
#undef MODEL_TEXT_STYLE

resource 'DLOG' (138) {
    { 70, 150, 490, 650 }, noGrowDocProc, visible, goAway, 0, 138,
    "Agent Question", centerMainScreen
};
resource 'dlgx' (138) { versionZero {
    kDialogFlagsUseThemeBackground | kDialogFlagsUseThemeControls
} };
resource 'DITL' (138) { {
    { 12, 16, 29, 484 }, StaticText { enabled, "" };
    { 34, 16, 126, 484 }, UserItem { enabled };
    { 130, 16, 146, 484 }, StaticText { enabled, "" };
    { 0, 0, 0, 0 }, StaticText { disabled, "" };
    { 282, 152, 303, 484 }, EditText { enabled, "" };
    { 310, 16, 348, 484 }, StaticText { enabled, "" };
    { 350, 16, 378, 484 }, StaticText { enabled, "" };
    { 386, 16, 408, 84 }, Button { enabled, "Back" };
    { 386, 96, 408, 168 }, Button { enabled, "Later" };
    { 386, 180, 408, 252 }, Button { enabled, "Stop" };
    { 386, 380, 408, 484 }, Button { enabled, "Answer" };
} };

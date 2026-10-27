/* Interactive agent questions: a modeless native Dialog Manager window.
 * The chat draft stays in its own TextEdit record. Nothing here sends P:. */
#include <Controls.h>
#include <ControlDefinitions.h>
#include <Fonts.h>
#include <TextEdit.h>
#include <TextUtils.h>
#include <Scrap.h>
#include <OSUtils.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "ToolQuestions.h"
#define QUESTION_CAP 8
#define OPTION_CAP 32
#define VISIBLE_OPTIONS 6
typedef struct { char label[192], description[512]; Boolean selected; } Option;
typedef struct {
    char header[64], prompt[2049], customText[256];
    Option options[OPTION_CAP]; short count;
    Boolean multiple, custom, customSelected;
} Question;
static Question questions[QUESTION_CAP];
static char requestID[104];
static short count, current, offset;
static Boolean received, submitting;
static DialogPtr dialog;
static UserItemUPP promptDrawing;
static ControlActionUPP scrollAction;
static ControlActionUPP promptScrollAction;
static ControlHandle choices[VISIBLE_OPTIONS], customControl, scrollbar;
static ControlHandle promptScrollbar;
static TEHandle promptText;
static short promptOffset;
static void (*sendLine)(const char *);

static TEHandle custom_te(void) {
#ifndef mc68000
    ControlHandle control = NULL; TEHandle te = NULL; Size actual = 0;
    /* Appearance dialogs can give the edit item its own control/TE record;
     * DialogRecord.textH then describes the legacy field, not the live one. */
    if (dialog && GetDialogItemAsControl(dialog,5,&control) == noErr && control &&
        GetControlData(control,kControlEntireControl,kControlEditTextTEHandleTag,sizeof te,&te,&actual) == noErr && te)
        return te;
#endif
    return dialog ? ((DialogPeek)dialog)->textH : NULL;
}

static void text(short item, const char *s) {
    DialogItemType type; Handle h; Rect r; Str255 p; long n = strlen(s);
    if (n > 255) n = 255; p[0] = n; memcpy(p+1,s,n);
    GetDialogItem(dialog,item,&type,&h,&r); SetDialogItemText(h,p);
}
static void decode(char *out, const char *in, short cap) {
    short n = 0; char c;
    while (*in && n < cap-1) {
        c = *in++;
        if (c == '\\' && *in) { c = *in++; if (c == 'n') c = '\r'; else if (c == 't') c = ' '; }
        out[n++] = c;
    }
    out[n] = 0;
}
static void encode(char *out, const char *in) {
    while (*in) { char c = *in++; if (c == '\\') { *out++ = '\\'; *out++ = '\\'; }
        else if (c == '\r' || c == '\n') { *out++ = '\\'; *out++ = 'n'; }
        else if (c == '\t') { *out++ = '\\'; *out++ = 't'; } else *out++ = c; }
    *out = 0;
}
static Boolean answered(short index) {
    Question *q = &questions[index]; short i;
    if (q->customSelected && q->customText[0]) return TRUE;
    for (i=0;i<q->count;i++) if (q->options[i].selected) return TRUE;
    return FALSE;
}
static void controls(void) {
    DialogItemType type; Handle h; Rect r;
    GetDialogItem(dialog,8,&type,&h,&r); HiliteControl((ControlHandle)h, current && !submitting ? 0 : 255);
    GetDialogItem(dialog,11,&type,&h,&r);
    SetControlTitle((ControlHandle)h,current+1<count ? "\pNext" : "\pAnswer");
    HiliteControl((ControlHandle)h, answered(current) && !submitting ? 0 : 255);
}
static Boolean save_custom(void) {
    DialogItemType type; Handle h; Rect r; Str255 p; Question *q; short i;
    if (!dialog || !count) return FALSE;
    q = &questions[current];
    /* The active field lives in the Dialog Manager's TE record. Its DITL
     * handle is synchronized only when focus leaves it, not on every key. */
    if (custom_te()) {
        TEHandle te = custom_te();
        short len = (*te)->teLength; if (len > 255) len = 255;
        p[0] = len; HLock((*te)->hText); memcpy(p+1,*(*te)->hText,len); HUnlock((*te)->hText);
    } else { GetDialogItem(dialog,5,&type,&h,&r); GetDialogItemText(h,p); }
    if (p[0] == strlen(q->customText) && !memcmp(p+1,q->customText,p[0])) return FALSE;
    memcpy(q->customText,p+1,p[0]); q->customText[p[0]] = 0;
    if (q->custom && q->customText[0]) {
        q->customSelected = TRUE;
        if (!q->multiple) for (i=0;i<q->count;i++) q->options[i].selected = FALSE;
    }
    return TRUE;
}
static void choice_titles(void) {
    Question *q = &questions[current]; short i, index, n; Str255 label;
    for (i=0;i<VISIBLE_OPTIONS;i++) {
        index = offset+i;
        if (index >= q->count) { HideControl(choices[i]); continue; }
        n = strlen(q->options[index].label); if (n > 66) n = 66;
        label[0] = n; memcpy(label+1,q->options[index].label,n);
        if (n < strlen(q->options[index].label) && n > 3) label[n] = label[n-1] = label[n-2] = '.';
        SetControlTitle(choices[i],label); SetControlValue(choices[i],q->options[index].selected);
        HiliteControl(choices[i],submitting ? 255 : 0); ShowControl(choices[i]);
    }
    SetControlValue(customControl,q->customSelected);
    HiliteControl(customControl,submitting ? 255 : 0);
}
static pascal void draw_prompt(DialogPtr d, short item) {
    DialogItemType type; Handle h; Rect r; GrafPtr saved;
    GetPort(&saved); SetPort((GrafPtr)d); GetDialogItem(d,item,&type,&h,&r);
    TextFont(kFontIDGeneva); TextSize(12); TextFace(0); ForeColor(blackColor);
    EraseRect(&r); if (promptText) TEUpdate(&r,promptText); SetPort(saved);
}
static pascal void scroll_prompt(ControlHandle control, short part) {
    short value = GetControlValue(control), step = promptText ? (*promptText)->lineHeight : 14;
    if (part == kControlUpButtonPart) value -= step;
    else if (part == kControlDownButtonPart) value += step;
    else if (part == kControlPageUpPart) value -= 78;
    else if (part == kControlPageDownPart) value += 78;
    if (value < 0) value = 0; if (value > GetControlMaximum(control)) value = GetControlMaximum(control);
    SetControlValue(control,value);
    if (promptText && value != promptOffset) { TEScroll(0,promptOffset-value,promptText); promptOffset = value; }
}
static void page(void) {
    Question *q = &questions[current]; short i, maximum = 0; Rect r; char heading[128]; FontInfo metrics;
    if (promptText) TEDispose(promptText); promptText = NULL; promptOffset = 0;
    SetRect(&r,16,34,462,126); TextFont(kFontIDGeneva); TextSize(12); TextFace(0); GetFontInfo(&metrics);
    promptText = TENew(&r,&r);
    if (promptText) {
        (*promptText)->txFont = kFontIDGeneva; (*promptText)->txSize = 12;
        (*promptText)->lineHeight = metrics.ascent+metrics.descent+metrics.leading;
        TESetText(q->prompt,strlen(q->prompt),promptText);
        maximum = (*promptText)->nLines * (*promptText)->lineHeight - (r.bottom-r.top);
        if (maximum < 0) maximum = 0;
    }
    SetControlMaximum(promptScrollbar,maximum); SetControlValue(promptScrollbar,0);
#ifndef mc68000
    SetControlViewSize(promptScrollbar,r.bottom-r.top);
#endif
    if (maximum) ShowControl(promptScrollbar); else HideControl(promptScrollbar);
    snprintf(heading,sizeof heading,"%s (%d of %d)",q->header,current+1,count); text(1,heading);
    text(3,q->multiple ? "Choose one or more answers, or write your own." : "Choose an answer, or write your own.");
    if (!q->custom) text(3,q->multiple ? "Choose one or more answers." : "Choose one answer.");
    for (i=0;i<VISIBLE_OPTIONS;i++) {
        if (choices[i]) DisposeControl(choices[i]);
        SetRect(&r,16,150+i*21,462,170+i*21);
        choices[i] = NewControl((WindowPtr)dialog,&r,"\p",TRUE,0,0,1,q->multiple ? checkBoxProc : radioButProc,0);
    }
    if (customControl) DisposeControl(customControl);
    SetRect(&r,16,284,147,302);
    customControl = NewControl((WindowPtr)dialog,&r,"\pYour answer:",q->custom,q->customSelected,0,1,q->multiple ? checkBoxProc : radioButProc,0);
    offset = 0; SetControlMaximum(scrollbar,q->count > VISIBLE_OPTIONS ? q->count - VISIBLE_OPTIONS : 0); SetControlValue(scrollbar,0);
    if (q->count > VISIBLE_OPTIONS) ShowControl(scrollbar); else HideControl(scrollbar);
    if (q->custom) { ShowDialogItem(dialog,5); text(5,q->customText); }
    else HideDialogItem(dialog,5);
    text(6,""); text(7,"Your chat draft is kept. Later leaves the question waiting; Stop cancels the reply.");
    choice_titles(); controls(); InvalRect(&((GrafPtr)dialog)->portRect);
}
static pascal void scroll_options(ControlHandle control, short part) {
    short value = GetControlValue(control);
    if (part == kControlUpButtonPart || part == kControlPageUpPart) value--;
    else if (part == kControlDownButtonPart || part == kControlPageDownPart) value++;
    if (value < 0) value = 0; if (value > GetControlMaximum(control)) value = GetControlMaximum(control);
    SetControlValue(control,value);
    if (offset != value) { offset = value; choice_titles(); }
}
static void close_dialog(void) {
    short i; GrafPtr port; if (!dialog) return; save_custom(); GetPort(&port);
    for (i=0;i<VISIBLE_OPTIONS;i++) choices[i] = NULL;
    customControl = scrollbar = promptScrollbar = NULL;
    if (promptText) TEDispose(promptText); promptText = NULL;
    DisposeDialog(dialog);
    if (port == (GrafPtr)dialog && FrontWindow()) SetPort(FrontWindow()); dialog = NULL;
    if (promptDrawing) DisposeUserItemUPP(promptDrawing); promptDrawing = NULL;
    if (scrollAction) DisposeControlActionUPP(scrollAction); scrollAction = NULL;
    if (promptScrollAction) DisposeControlActionUPP(promptScrollAction); promptScrollAction = NULL;
}
void ToolQuestionsInit(void (*send)(const char *)) { sendLine = send; }
Boolean ToolQuestionsPending(void) { return requestID[0] != 0 && count > 0; }
Boolean ToolQuestionsIsOpen(void) { return dialog != NULL; }
const char *ToolQuestionsID(void) { return requestID; }
short ToolQuestionsIndex(void) { return current; }
short ToolQuestionsCustomBytes(void) { return count ? strlen(questions[current].customText) : 0; }
short ToolQuestionsEditBytes(void) { TEHandle te = custom_te(); return te ? (*te)->teLength : 0; }
void ToolQuestionsReset(void) { close_dialog(); requestID[0] = 0; count = current = offset = 0; received = submitting = FALSE; memset(questions,0,sizeof questions); }
void ToolQuestionsOpen(void) {
    GrafPtr saved; DialogItemType type; Handle h; Rect r;
    if (!ToolQuestionsPending()) return;
    if (dialog) { if (IsWindowCollapsed((WindowPtr)dialog)) CollapseWindow((WindowPtr)dialog,FALSE); ShowWindow((WindowPtr)dialog); SelectWindow((WindowPtr)dialog); return; }
    GetPort(&saved); dialog = GetNewDialog(138,NULL,(WindowPtr)-1); if (!dialog) return;
    SetPort((GrafPtr)dialog); GetDialogItem(dialog,2,&type,&h,&r);
    promptDrawing = NewUserItemUPP(draw_prompt); SetDialogItem(dialog,2,userItem,(Handle)promptDrawing,&r);
    SetRect(&r,468,150,484,276); scrollbar = NewControl((WindowPtr)dialog,&r,"\p",FALSE,0,0,0,kControlScrollBarLiveProc,0);
    SetRect(&r,468,34,484,126); promptScrollbar = NewControl((WindowPtr)dialog,&r,"\p",FALSE,0,0,0,kControlScrollBarLiveProc,0);
    promptScrollAction = NewControlActionUPP(scroll_prompt);
    scrollAction = NewControlActionUPP(scroll_options); page(); SetDialogDefaultItem(dialog,11);
    ShowWindow((WindowPtr)dialog); SelectWindow((WindowPtr)dialog); SetPort(saved);
}
void ToolQuestionsLine(char *p) {
    char *fields[6]; short n = 1, i, j; Question *q;
    fields[0] = p;
    while (n < 6) { char *tab = strchr(fields[n-1],'\t'); if (!tab) break; *tab = 0; fields[n++] = tab+1; }
    if (!strcmp(fields[0],"BEGIN") && n == 3) {
        ToolQuestionsReset(); count = atoi(fields[2]);
        if (count < 1 || count > QUESTION_CAP || strlen(fields[1]) >= sizeof requestID) { ToolQuestionsReset(); return; }
        strcpy(requestID,fields[1]);
    } else if (!strcmp(fields[0],"QUESTION") && n == 6 && ToolQuestionsPending()) {
        i = atoi(fields[1]); if (i < 0 || i >= count) return; q = &questions[i];
        q->multiple = atoi(fields[2]) != 0; q->custom = atoi(fields[3]) != 0;
        decode(q->header,fields[4],sizeof q->header); decode(q->prompt,fields[5],sizeof q->prompt);
    } else if (!strcmp(fields[0],"OPTION") && n == 5 && ToolQuestionsPending()) {
        i = atoi(fields[1]); j = atoi(fields[2]); if (i < 0 || i >= count || j < 0 || j >= OPTION_CAP) return;
        q = &questions[i]; decode(q->options[j].label,fields[3],sizeof q->options[j].label);
        decode(q->options[j].description,fields[4],sizeof q->options[j].description); if (q->count <= j) q->count = j+1;
    } else if (!strcmp(fields[0],"END") && n == 2 && !strcmp(fields[1],requestID)) received = TRUE;
    else if (!strcmp(fields[0],"CLEAR") && n == 2 && !strcmp(fields[1],requestID)) ToolQuestionsReset();
    else if (!strcmp(fields[0],"ERROR") && n == 2) {
        submitting = FALSE;
        if (dialog) { char s[256]; GrafPtr saved; GetPort(&saved); SetPort((GrafPtr)dialog);
            decode(s,fields[1],sizeof s); text(7,s); choice_titles(); controls(); SetPort(saved); }
    }
}
void ToolQuestionsTick(Boolean online) {
    GrafPtr saved;
    if (!online) { if (ToolQuestionsPending()) ToolQuestionsReset(); return; }
    if (received) { received = FALSE; ToolQuestionsOpen(); }
    if (dialog) { GetPort(&saved); SetPort((GrafPtr)dialog); if (save_custom()) { choice_titles(); controls(); } SetPort(saved); }
}
static void next(void) {
    char line[700], escaped[520]; short i, j;
    save_custom(); if (submitting || !answered(current)) return;
    if (current+1 < count) { current++; page(); return; }
    for (i=0;i<count;i++) if (!answered(i)) { current = i; page(); return; }
    submitting = TRUE; snprintf(line,sizeof line,"I:REPLY\t%s",requestID); sendLine(line);
    for (i=0;i<count;i++) {
        for (j=0;j<questions[i].count;j++) if (questions[i].options[j].selected) {
            snprintf(line,sizeof line,"I:ANSWER\t%s\t%d\tO\t%d",requestID,i,j); sendLine(line);
        }
        if (questions[i].customSelected && questions[i].customText[0]) {
            encode(escaped,questions[i].customText); snprintf(line,sizeof line,"I:ANSWER\t%s\t%d\tC\t%s",requestID,i,escaped); sendLine(line);
        }
    }
    snprintf(line,sizeof line,"I:SUBMIT\t%s",requestID); sendLine(line); text(7,"Sending your answer..."); choice_titles(); controls();
}
Boolean ToolQuestionsEvent(EventRecord *e) {
    WindowPtr w = NULL; GrafPtr saved; DialogPtr selected; short part, hit = 0, i, ch; ControlHandle control = NULL; Point p; Question *q;
    if (!dialog) return FALSE;
    if (e->what == updateEvt && (WindowPtr)e->message == (WindowPtr)dialog) {
        GetPort(&saved); SetPort((GrafPtr)dialog); BeginUpdate((WindowPtr)dialog); DrawDialog(dialog); DrawControls((WindowPtr)dialog); EndUpdate((WindowPtr)dialog); SetPort(saved); return TRUE;
    }
    if ((e->what == keyDown || e->what == autoKey) && FrontWindow() == (WindowPtr)dialog) {
        ch = e->message & charCodeMask; GetPort(&saved); SetPort((GrafPtr)dialog);
        if (ch == 27 || ((e->modifiers & cmdKey) && (ch == 'w' || ch == 'W'))) { SetPort(saved); close_dialog(); return TRUE; }
        if (ch == '\r' || ch == '\n') { if (e->what == keyDown) next(); SetPort(saved); return TRUE; }
        if (e->modifiers & cmdKey) {
            if (ch == 'a') SelectDialogItemText(dialog,5,0,32767);
            else if (ch == 'c' || ch == 'x') { if (ch == 'c') DialogCopy(dialog); else DialogCut(dialog); ZeroScrap(); TEToScrap(); }
            else if (ch == 'v' && !submitting && questions[current].custom) { Str255 s; DialogItemType type; Handle h; Rect r; TEFromScrap(); DialogPaste(dialog); GetDialogItem(dialog,5,&type,&h,&r); GetDialogItemText(h,s); for (i=1;i<=s[0];i++) if (s[i] < ' ' || s[i] > '~') s[i] = ' '; SetDialogItemText(h,s); }
            SetPort(saved); return TRUE;
        }
        /* Keep the same TextEdit byte restriction as the composer. */
        if (submitting || !questions[current].custom || (e->modifiers & 0x0800) || !((ch >= ' ' && ch <= '~') || ch == '\b' || ch == 0x7f) || (custom_te() && (*custom_te())->teLength >= 255 && ch != '\b' && ch != 0x7f)) { SetPort(saved); return TRUE; }
        DialogSelect(e,&selected,&hit);
        if (save_custom()) { choice_titles(); controls(); }
        SetPort(saved); return TRUE;
    }
    if (e->what == nullEvent && FrontWindow() == (WindowPtr)dialog) {
        GetPort(&saved); SetPort((GrafPtr)dialog); if (IsDialogEvent(e)) DialogSelect(e,&selected,&hit); SetPort(saved); return FALSE;
    }
    if (e->what != mouseDown) return FALSE;
    part = FindWindow(e->where,&w); if (part == inMenuBar || w != (WindowPtr)dialog) return FALSE;
    if (part == inGoAway) { if (TrackGoAway(w,e->where)) close_dialog(); return TRUE; }
    if (part == inDrag) { DragWindow(w,e->where,&qd.screenBits.bounds); return TRUE; }
    if (part == inCollapseBox) { if (TrackBox(w,e->where,part)) CollapseWindow(w,!IsWindowCollapsed(w)); return TRUE; }
    if (part != inContent) return TRUE;
    SelectWindow(w); GetPort(&saved); SetPort(w); p = e->where; GlobalToLocal(&p); FindControl(p,w,&control); q = &questions[current];
    if (control && control == promptScrollbar) { TrackControl(control,p,promptScrollAction); scroll_prompt(control,kControlIndicatorPart); SetPort(saved); return TRUE; }
    if (control && control == scrollbar) { TrackControl(control,p,scrollAction); scroll_options(control,kControlIndicatorPart); SetPort(saved); return TRUE; }
    for (i=0;i<VISIBLE_OPTIONS;i++) if (control && control == choices[i] && !submitting && i+offset < q->count) {
        if (TrackControl(control,p,NULL)) {
            short j, index = i+offset; Boolean chosen = q->options[index].selected;
            if (!q->multiple) { for (j=0;j<q->count;j++) q->options[j].selected = FALSE; q->customSelected = FALSE; }
            q->options[index].selected = q->multiple ? !chosen : TRUE;
            text(6,q->options[index].description[0] ? q->options[index].description : q->options[index].label); choice_titles(); controls();
        }
        SetPort(saved); return TRUE;
    }
    if (control && control == customControl && !submitting) {
        if (TrackControl(control,p,NULL)) {
            if (!q->multiple) { for (i=0;i<q->count;i++) q->options[i].selected = FALSE; q->customSelected = TRUE; } else q->customSelected = !q->customSelected;
            SelectDialogItemText(dialog,5,0,32767); choice_titles(); controls();
        }
        SetPort(saved); return TRUE;
    }
    if (DialogSelect(e,&selected,&hit)) {
        if (hit == 8 && current && !submitting) { save_custom(); current--; page(); }
        else if (hit == 9) { SetPort(saved); close_dialog(); return TRUE; }
        else if (hit == 10) { sendLine("A:"); }
        else if (hit == 11) next();
    }
    SetPort(saved); return TRUE;
}

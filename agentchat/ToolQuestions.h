#ifndef AGENTCHAT_TOOL_QUESTIONS_H
#define AGENTCHAT_TOOL_QUESTIONS_H
#include <Dialogs.h>
void ToolQuestionsInit(void (*send)(const char *));
void ToolQuestionsLine(char *payload);
void ToolQuestionsTick(Boolean connected);
Boolean ToolQuestionsEvent(EventRecord *event);
Boolean ToolQuestionsPending(void);
Boolean ToolQuestionsIsOpen(void);
const char *ToolQuestionsID(void);
short ToolQuestionsIndex(void);
short ToolQuestionsCustomBytes(void);
short ToolQuestionsEditBytes(void);
void ToolQuestionsOpen(void);
void ToolQuestionsReset(void);
#endif

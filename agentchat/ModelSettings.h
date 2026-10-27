#ifndef AGENTCHAT_MODEL_SETTINGS_H
#define AGENTCHAT_MODEL_SETTINGS_H
#include <Dialogs.h>
typedef void (*ModelSettingsSendProc)(const char *);
void ModelSettingsInit(ModelSettingsSendProc send);
void ModelSettingsLine(char *payload);
void ModelSettingsOpen(void);
void ModelSettingsClose(void);
Boolean ModelSettingsIsOpen(void);
void ModelSettingsTick(Boolean connected);
Boolean ModelSettingsEvent(EventRecord *event);
#endif

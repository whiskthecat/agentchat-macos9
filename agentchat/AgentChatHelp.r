/* Classic System 7-9 Balloon Help. The OS owns layout, tracking and drawing.
 * Keep dialog entries in DITL order and menu entries in build_menu order.
 * The first hmnu entry describes the menu title (item zero).
 */
#include "Balloons.r"
#include "AgentChatHelp.h"

resource 'STR#' (kAgentHelpStrings, "Window Balloon Help", purgeable) { {
    /* kHelpSend */
    "Send this message to the agent.\r\rReturn also sends; Shift-Return starts a new line.";
    "Send this message to the agent.\r\rNot available now because the message is empty. Type a message first.";
    "Send this message to the agent.\r\rNot available while offline. Use Reconnect or Connection Settings to connect to the bridge.";
    "Send this message to the agent.\r\rNot available while the agent is replying. Wait for the reply to finish, or click Stop.";
    "AgentChat is loading a conversation. Please wait for its messages to arrive before sending or changing conversations.";
    "Stop the agent's current reply. Text already received stays in the conversation.\r\rCommand-period also stops a reply.";
    "Stop the agent's current reply.\r\rNot available now because there is no reply in progress.";
    "Type your message here, up to 2048 characters, or paste a copied picture.\r\rReturn sends; Shift-Return starts a new line. Each conversation keeps its caption and picture draft while AgentChat is open.";
    "Messages in this conversation. Drag across text to select it, then use Copy. Click a message heading to copy the whole message.\r\rCommand-F finds text; Command-Shift-C copies the whole transcript.";
    "Scroll through this conversation. Drag the thumb or click the arrows. Returning to the bottom follows new messages automatically.";
    "Messages are sent as plain text. These formatting symbols are not available yet.\r\rUse Shift-Return to separate paragraphs.";
    "Reconnect to the bridge using your saved connection settings.\r\rThis keeps your conversations and unsent drafts.";
    "Not available while a reply or conversation is loading. Wait for it to finish, or stop the current reply first.";
    "Start a new conversation. It appears in the saved history after you send its first message.\r\rCommand-N also starts a new chat.";
    "Start a new conversation.\r\rNot available while offline. Connect to the bridge first.";
    "Choose the bridge address, chat port, update port, and whether to connect at startup.\r\rCommand-comma also opens Connection Settings.";
    "Select a conversation to read its messages and continue chatting. Unsent drafts are kept while AgentChat is open.\r\rCommand-R refreshes history; Command-E renames the current conversation.";
    "Scroll the list of saved conversations. Select a name to open that conversation.";
    "Shows the connection status and AgentChat version.\r\rUse Reconnect to try again, or Connection Settings to change the bridge address.";
    "Choose a model contact to reopen its last conversation. Each contact has its own history and new-chat draft.\r\rNew Chat starts a conversation with the selected contact.";
    "Scroll the available model contacts. The list comes from the connected bridge.";
    "The picture is being transferred to the bridge. Please wait before sending. Uploading a picture does not send a message to the model.";
    "This model does not accept pictures. Choose an image-capable model for a new picture conversation, or remove this picture to send the text.";
    "Remove the picture from this unsent message. The caption stays in the message field.";
    "This picture will be sent with your message. You can add a caption, or send the picture by itself. Remove clears the picture from the draft.";
} };

/* Inline resource strings keep menus/dialogs localizable without C changes. */
#define MENU_HELP(text) HMStringItem { text, text, text, text }
#define MENU_STATES(text, dimmed) HMStringItem { text, dimmed, text, text }
#define DIALOG_HELP(text) HMStringItem { { 10, 10 }, { 0, 0, 0, 0 }, text, text, text, text }

resource 'hmnu' (1, "Apple menu help", purgeable) {
    HelpMgrVersion, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        MENU_HELP("Information about AgentChat and the desk accessories installed on this Macintosh.");
        MENU_HELP("Show the AgentChat version and a brief introduction.");
    }
};
resource 'hmnu' (2, "File menu help", purgeable) {
    HelpMgrVersion, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        MENU_HELP("Save a transcript, start a conversation, change connection settings, or quit AgentChat.");
        MENU_HELP("Save the current transcript as a text file using the Macintosh file dialog.\r\rCommand-S also saves.");
        HMSkipItem {};
        MENU_HELP("Quit AgentChat. Saved conversations remain on the bridge. Unsent drafts are kept only while the app is open.");
        HMSkipItem {};
        MENU_STATES("Start a new conversation. It is saved after you send the first message.", "Start a new conversation. Not available while offline, replying, or loading a conversation.");
        MENU_STATES("Change the bridge address, ports, and startup connection setting.", "Change connection settings. Not available while a reply or conversation is loading.");
        MENU_HELP("Choose which automatically discovered models appear in the sidebar. Command-M also opens Models.");
    }
};
resource 'hmnu' (4, "Edit menu help", purgeable) {
    HelpMgrVersion, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        MENU_HELP("Edit your draft, copy messages, or find text in the conversation.");
        MENU_STATES("Remove the selected draft text and put it on the Clipboard.", "Cut selected draft text. Not available now because no draft text is selected.");
        MENU_STATES("Put the selected text or conversation message on the Clipboard.", "Copy text or a message. Select draft text or click a conversation message first.");
        MENU_STATES("Paste Clipboard text or a copied picture into your draft. Text is limited to 2048 characters.", "Paste text or a picture into your draft. Click the message field first.");
        MENU_STATES("Select all the text in your draft or the chat history.", "Select all text. Click the message field or chat history first.");
        HMSkipItem {};
        MENU_HELP("Copy the entire transcript, including speaker names and paragraph breaks.\r\rCommand-Shift-C also copies the transcript.");
        MENU_HELP("Find text in the current transcript. The search ignores letter case and wraps to the beginning.\r\rCommand-F also opens Find.");
        MENU_HELP("Find the next occurrence of the last search text.\r\rCommand-G also finds the next occurrence.");
    }
};
resource 'hmnu' (200, "Chat menu help", purgeable) {
    HelpMgrVersion, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        MENU_HELP("Manage your connection, saved conversations, replies, and app updates.");
        MENU_HELP("Connect to the saved bridge address, or disconnect if already connected.\r\rCommand-K also changes the connection.");
        MENU_HELP("Send a screenshot of the Macintosh screen to the bridge.");
        MENU_HELP("Fetch a new AgentChat build from the update server and relaunch the app.\r\rCommand-U also checks for an update.");
        MENU_HELP("Quit AgentChat. Saved conversations remain on the bridge.");
        MENU_STATES("Refresh the conversation list from the bridge.", "Refresh saved history. Not available while offline, replying, or loading a conversation.");
        MENU_STATES("Change the name of the current saved conversation.", "Rename the current conversation. Select a saved conversation while connected and wait for any reply to finish.");
        MENU_STATES("Stop the current reply and keep the text already received.", "Stop the current reply. Not available now because no connected reply is in progress.");
        MENU_STATES("Permanently delete the current saved conversation and its messages after confirmation.", "Delete a saved conversation. Select one while connected and wait for any reply or loading to finish.");
        MENU_HELP("Load a development transcript to check scrolling and message layout. Available in the VM test build.");
    }
};
resource 'hmnu' (300, "Window menu help", purgeable) {
    HelpMgrVersion, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        MENU_HELP("Show the conversation list or bring the chat window to the front.");
        MENU_HELP("Show or hide the navigation window containing connection actions and saved conversations.\r\rCommand-L also changes its visibility.");
        MENU_HELP("Bring the chat window to the front.");
        MENU_HELP("Restore the navigation and chat windows to their usual size and position, and bring hidden windows back onto the desktop.\r\rCommand-0 also arranges the windows.");
    }
};

resource 'hdlg' (130, "Update dialog help", purgeable) {
    HelpMgrVersion, 0, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        DIALOG_HELP("Name of the AgentChat update file on the build server. The app fetches both its program and resource forks.");
        DIALOG_HELP("Fetch this update and relaunch AgentChat.\r\rReturn also chooses OK.");
        DIALOG_HELP("Close this dialog without updating.\r\rEscape also cancels.");
        DIALOG_HELP("Enter the update file supplied by your build server.");
    }
};
resource 'hdlg' (131, "About dialog help", purgeable) {
    HelpMgrVersion, 0, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        DIALOG_HELP("Close the About AgentChat window.");
        DIALOG_HELP("AgentChat is a native Macintosh chat client for a coding agent reached through your bridge.");
        DIALOG_HELP("The version of AgentChat currently running on this Macintosh.");
        DIALOG_HELP("AgentChat sends messages to a coding agent through a bridge on your network.");
        DIALOG_HELP("AgentChat uses the Macintosh Toolbox and runs on PowerPC Mac OS 9.");
        DIALOG_HELP("Return sends your message. Shift-Return starts a new line in the message field.");
    }
};
resource 'hdlg' (132, "Rename dialog help", purgeable) {
    HelpMgrVersion, 0, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        DIALOG_HELP("Enter a name for the current conversation. Its messages stay in the conversation.");
        DIALOG_HELP("Save this conversation name on the bridge and update the list.\r\rReturn also chooses Rename.");
        DIALOG_HELP("Keep the current name and close this dialog.\r\rEscape also cancels.");
        DIALOG_HELP("A name that helps you find this conversation in the list.");
    }
};
resource 'hdlg' (133, "Find dialog help", purgeable) {
    HelpMgrVersion, 0, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        DIALOG_HELP("Enter text to find in the current transcript. The search ignores letter case and wraps to the beginning.");
        DIALOG_HELP("Select and reveal the next matching message.\r\rCommand-G repeats the search after this dialog closes.");
        DIALOG_HELP("Close this dialog without searching.\r\rEscape also cancels.");
        DIALOG_HELP("Find text in the conversation. Command-G repeats your last search.");
    }
};
resource 'hdlg' (135, "Connection dialog help", purgeable) {
    HelpMgrVersion, 0, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        DIALOG_HELP("IPv4 address of the relay computer. Leave blank to find a relay on your local network. A saved address is always tried before automatic discovery.");
        DIALOG_HELP("Save these settings in the Macintosh Preferences folder and connect to this bridge.\r\rReturn also chooses Connect.");
        DIALOG_HELP("Close this dialog without changing the saved settings.\r\rEscape also cancels.");
        DIALOG_HELP("The network address of your bridge computer. Enter an IPv4 address in the field beside this label.");
        DIALOG_HELP("TCP port used for chat and conversation history. Enter a number from 1 to 65535 that matches the bridge's chat port.");
        DIALOG_HELP("The bridge's TCP chat port. This is separate from the HTTP port used for app updates.");
        DIALOG_HELP("HTTP port of the server supplying AgentChat updates. Enter a number from 1 to 65535. This is separate from the chat port.");
        DIALOG_HELP("The HTTP server port used to download a new app build.");
        DIALOG_HELP("Choose whether AgentChat connects automatically at startup. When unchecked, use Reconnect or Command-K to connect.");
        DIALOG_HELP("Explains any invalid address or port. Correct the indicated field before connecting.");
        DIALOG_HELP("Fill in this build's default address, ports, and startup setting. Choose Connect to save them, or Cancel to keep your current settings.");
        DIALOG_HELP("Address and ports of the computer running your bridge. Enter an IPv4 address and the matching service ports.");
        DIALOG_HELP("Automatic connection and local network discovery settings.");
        DIALOG_HELP("Choose which models from connected providers appear in the sidebar.");
        DIALOG_HELP("Find a relay on the local network when no address is set or the saved address fails. Turn this off to use only your typed address and send no discovery packets.");
        DIALOG_HELP("Search the local network now, then connect and save the first responding relay that accepts a connection. Requires discovery to be enabled. This closes Connection Settings.");
    }
};

resource 'hdlg' (136, "Delete confirmation help", purgeable) {
    HelpMgrVersion, 0, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        DIALOG_HELP("The conversation you are about to delete. Its saved messages and unsent draft will be removed.");
        DIALOG_HELP("Permanently delete this conversation and its messages from the bridge. This cannot be undone.");
        DIALOG_HELP("Keep the conversation and its draft. Return, Escape, or Command-period also cancels.");
        DIALOG_HELP("Deletion removes the saved conversation from the bridge. Other conversations are kept.");
    }
};

resource 'hdlg' (137, "Models window help", purgeable) {
    HelpMgrVersion, 0, hmDefaultOptions, 0, 0, HMSkipItem {}, {
        DIALOG_HELP("Click a model's checkbox to show or hide it in the sidebar. Changes are saved immediately. Shown models are listed first on opening or refreshing.");
        DIALOG_HELP("Close Models. Changes are already saved. Return, Escape and Command-W also close this window.");
        DIALOG_HELP("Remove remembered visibility overrides and use provider suggestions. This takes effect immediately.");
        DIALOG_HELP("Refresh connected provider catalogs without sending any inference request. Shown models are placed first.");
        DIALOG_HELP("Models are discovered from connected providers. New models follow automatic provider suggestions.");
        DIALOG_HELP("The provider model identifier of the selected row.");
        DIALOG_HELP("The provider that supplies this model and its automatic visibility suggestion.");
        DIALOG_HELP("Model discovery status and visibility information.");
    }
};

#undef MENU_HELP
#undef MENU_STATES
#undef DIALOG_HELP

"""OpenCode's interactive questions, kept separate from chat prompts.

Native replies carry option indices; the relay supplies the original labels.
Only a complete, validated reply to the current request reaches the upstream.
"""
import re
import threading

MAX_QUESTIONS = 8
MAX_OPTIONS = 32


def escape(value):
    return str(value).replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n").replace("\t", "\\t")


def unescape(value):
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "\\": "\\"}.get(m[1], m[1]), value)


class ToolQuestions:
    def __init__(self, session, api):
        self.session, self.api = session, api
        self.pending = None
        self.answers = None
        self.lock = threading.RLock()

    def asked(self, request):
        with self.lock:
            if request.get("sessionID") != self.session.sid:
                return
            if self.pending and self.pending["id"] == request.get("id"):
                return
            questions = request.get("questions", [])
            if not re.fullmatch(r"que[A-Za-z0-9_-]{1,100}", request.get("id", "")):
                raise ValueError("Invalid question request ID")
            if not 1 <= len(questions) <= MAX_QUESTIONS:
                raise ValueError("Question request exceeds the client's eight-question limit")
            for q in questions:
                if not q.get("options") and not q.get("custom", True):
                    raise ValueError("Question has no available answers")
                if len(q.get("options", [])) > MAX_OPTIONS:
                    raise ValueError("Question exceeds the client's 32-choice limit")
                for key, limit in (("question", 2048), ("header", 63)):
                    if len(q.get(key, "").encode("mac_roman", "replace")) > limit:
                        raise ValueError("Question text exceeds the client's display limit")
                for option in q.get("options", []):
                    if len(option["label"].encode("mac_roman", "replace")) > 191 or len(option.get("description", "").encode("mac_roman", "replace")) > 511:
                        raise ValueError("Question choice exceeds the client's display limit")
            self.pending, self.answers = request, None
            self.session.inflight = True
            self.session.assistant_msg = (request.get("tool") or {}).get("messageID") or self.session.assistant_msg
            send = self.session.send
            send("I:BEGIN\t%s\t%d" % (request["id"], len(questions)))
            for i, q in enumerate(questions):
                send("I:QUESTION\t%d\t%d\t%d\t%s\t%s" % (i, bool(q.get("multiple")), q.get("custom", True), escape(q.get("header", "Question")), escape(q["question"])))
                for j, option in enumerate(q.get("options", [])):
                    send("I:OPTION\t%d\t%d\t%s\t%s" % (i, j, escape(option["label"]), escape(option.get("description", ""))))
            send("I:END\t" + request["id"])
            send("S:Answer needed")

    def recover(self):
        if not self.session.sid or self.session.model.startswith("codex/"):
            return
        try:
            requests = self.api("GET", "/question")
        except Exception:
            return  # Older servers do not expose the question API.
        for request in requests:
            if request.get("sessionID") == self.session.sid:
                try:
                    self.asked(request)
                except (ValueError, KeyError, TypeError) as error:
                    self.session.inflight = True
                    self.session.abort("unsupported recovered question")
                    self.session.send("X:" + str(error))
                    self.session.send("D:")
                break

    def clear(self, request_id=None):
        with self.lock:
            if not self.pending or (request_id and self.pending["id"] != request_id):
                return
            self.session.send("I:CLEAR\t" + self.pending["id"])
            self.pending, self.answers = None, None

    def line(self, payload):
        with self.lock:
            fields = payload.split("\t")
            try:
                if len(fields) < 2 or not self.pending or fields[1] != self.pending["id"]:
                    raise ValueError("This question is no longer pending")
                request = self.pending
                if fields[0] == "REPLY" and len(fields) == 2:
                    self.answers = [[] for _ in request["questions"]]
                elif fields[0] == "ANSWER" and len(fields) == 5 and self.answers is not None:
                    i = int(fields[2])
                    if not 0 <= i < len(self.answers):
                        raise ValueError("Unknown question")
                    q = request["questions"][i]
                    if fields[3] == "O":
                        index = int(fields[4])
                        if not 0 <= index < len(q.get("options", [])):
                            raise ValueError("Unknown choice")
                        answer = q["options"][index]["label"]
                    elif fields[3] == "C" and q.get("custom", True):
                        answer = unescape(fields[4]).strip()
                        if not answer or len(answer.encode("mac_roman", "replace")) > 255:
                            raise ValueError("Enter an answer of 1 to 255 characters")
                    else:
                        raise ValueError("Custom answers are unavailable for this question")
                    if answer not in self.answers[i]:
                        self.answers[i].append(answer)
                elif fields[0] == "SUBMIT" and len(fields) == 2 and self.answers is not None:
                    for q, answers in zip(request["questions"], self.answers):
                        if not answers or (not q.get("multiple") and len(answers) != 1):
                            raise ValueError("Answer every question; choose one answer for a single-choice question")
                    answers = self.answers
                    self.answers = None  # A retry must start a fresh reply.
                    self.api("POST", "/question/%s/reply" % request["id"], {"answers": answers})
                    self.clear(request["id"])
                    if self.session.inflight: self.session.send("S:thinking...")
                else:
                    raise ValueError("Invalid question reply sequence")
            except (ValueError, KeyError, TypeError, OSError) as e:
                self.answers = None
                self.session.send("I:ERROR\t" + escape(str(e)))

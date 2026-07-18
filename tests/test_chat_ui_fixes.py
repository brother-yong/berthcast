"""Locks in the three chat UI/UX fixes (18 Jul 2026) so they can't silently regress.

These are frontend JS behaviours inside templates/chat.html — there's no browser
harness in this suite, so (matching test_landing_minimal.py) we assert against the
template source directly:

  1. Reopening a chat from history drops the centered 'new chat' layout
     (loadConversation must clear the is-empty flag via hideWelcome()).
  2. The reply streams as PLAIN TEXT while forming (no per-token markdown re-parse,
     no trailing typing cursor); markdown is rendered once, on done.
  3. The <thinking> parser reads the whole accumulated stream (indexOf on rawText),
     so a tag split across network chunks can't leak raw reasoning into the answer.

Run: python tests/test_chat_ui_fixes.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHAT = os.path.join(ROOT, "templates", "chat.html")

with open(CHAT, "r", encoding="utf-8") as f:
    html = f.read()

F = []


def _check(cond, msg):
    print(("ok: " if cond else "FAIL: ") + msg)
    if not cond:
        F.append(msg)


# Isolate the loadConversation function body so the assertions are scoped to it.
m = re.search(r"async function loadConversation\(convId\)\s*\{(.*?)\n\}", html, re.DOTALL)
_check(m is not None, "loadConversation function found")
load_body = m.group(1) if m else ""

# 1) History reopen must leave the 'new chat' centered layout.
_check("hideWelcome()" in load_body,
       "loadConversation drops the is-empty layout (calls hideWelcome)")

# 2) Streaming renders plain text while forming, formats once at the end.
_check("bubbleEl.textContent = fullText" in html,
       "reply streams as plain text (textContent), not per-token markdown")
_check("renderMarkdown(fullText) + '<span class=\"typing-cursor\"></span>'" not in html,
       "the per-token markdown re-parse + trailing cursor is gone")
_check("bubbleEl.innerHTML = renderMarkdown(fullText);" in html,
       "markdown is still rendered once, on done")

# 4) Stream-failure handling: a partial answer must survive.
#    (a) Stream ends without an ev.done (deploy restart, proxy cut): finalize
#        the markdown render after the read loop instead of leaving raw text.
#    (b) Network error mid-stream: keep what already streamed, append an error
#        line — never wipe the bubble.
_check("finished = true" in html,
       "done/error branches mark the stream finished")
_check("if (!finished && fullText)" in html,
       "stream ending without done still finalizes the render")
_check("Network error. Please try again." not in html,
       "network error preserves the partial answer (old wipe is gone)")
_check("Connection lost" in html,
       "network error appends a connection-lost line instead")

# 3) Robust <thinking> parsing over the whole accumulated stream.
_check("rawText += ev.text" in html,
       "stream is accumulated into rawText before parsing")
_check("rawText.indexOf('<thinking>')" in html,
       "thinking split runs on the whole stream (tolerant of chunk-split tags)")
_check("chunk.includes('<thinking>')" not in html,
       "the fragile per-chunk tag detector is gone")

if F:
    print("\nSOME TESTS FAILED")
    sys.exit(1)
print("\nAll chat-UI-fix tests passed.")

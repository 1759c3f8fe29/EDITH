"""A real window to type into, so input synthesis can be verified by its effect.

``input.*`` verifies *delivery* -- which window received the keystrokes -- and says
plainly that whether the application acted on them cannot be observed from outside.
That is honest, and it is also the whole reason L11 was never exercised: on the live
session there is no application whose insides NEXUS may look at, and running the tool
would seize the user's keyboard mid-session.

Inside a nested X server there can be. This app writes what it actually received to a
JSON file, so the test asserts on the application's own state rather than on
``xdotool``'s exit code. It is the difference between "the keystrokes were sent" and
"the text is in the field".

It also publishes the on-screen rectangle of its button, from ``winfo_rootx`` and
friends. §12 asks for targeting that is not hard-coded pixels; a widget reporting
where it actually is stands in for the accessibility API a real application would
expose, and means the test still passes if the window manager places the window
somewhere else.

The same rectangles are what make window management checkable by effect rather than by
read-back. ``winfo_rootx``/``winfo_rooty`` are the application's own view of where the
X server has put it, so after a move or a resize the test can ask *the window* where it
ended up, instead of asking one of the tools that did the moving. That matters here more
than usual: the geometry readers everyone reaches for (``xdotool getwindowgeometry``,
``wmctrl -lG``) both misreport a reparented window's position, so a test built on them
would agree with a bug rather than catch it.

Note that ``window_id`` is Tk's *inner* window. Tk wraps every toplevel, so the window
the window manager manages -- the one that carries ``_NET_WM_STATE``, the one wmctrl
lists and the one ``getactivewindow`` returns -- is this window's parent, one id higher.
Tests resolve that separately; reading state from the id below it finds nothing at all.

Run as::

    DISPLAY=:<nested> python3 x11_probe_app.py /path/to/state.json [window-title] [wm-class]

``wm-class`` exists for the credential guard: a window can be identifiable *only* by
its class (title "Vault", class "Bitwarden"), and the guard has to catch that.
"""
from __future__ import annotations

import json
import os
import sys
import tkinter as tk
from pathlib import Path

STATE = Path(sys.argv[1])
TITLE = sys.argv[2] if len(sys.argv) > 2 else "NEXUS input probe"
WM_CLASS = sys.argv[3] if len(sys.argv) > 3 else None


class Probe:
    def __init__(self) -> None:
        # className is what X11 reports as the second half of WM_CLASS. Passing it is
        # the only way to build a window whose class and title disagree, which is the
        # case the credential guard has to survive.
        self.root = tk.Tk(className=WM_CLASS) if WM_CLASS else tk.Tk()
        self.root.title(TITLE)
        self.root.geometry("560x220+40+40")
        self.clicks = 0
        self.keys: list[str] = []

        self.entry = tk.Entry(self.root, width=48, name="field")
        self.entry.pack(pady=18)
        self.entry.focus_force()

        self.button = tk.Button(self.root, text="CONFIRM", command=self.clicked,
                                width=18, height=2, name="confirm")
        self.button.pack(pady=8)

        self.label = tk.Label(self.root, text="waiting", name="state")
        self.label.pack(pady=8)

        # Every named key the test presses is recorded, so "the shortcut arrived" is a
        # fact from inside the application rather than an inference.
        #
        # Tab is deliberately absent. Tk consumes it for focus traversal before any
        # binding here sees it -- measured, not assumed: three synthesised Tabs arrive
        # at the X server and zero reach this handler, while Escape and Return arrive
        # three for three. A test written on Tab would look like a broken tool.
        for seq, name in (
            ("<Return>", "Return"), ("<Escape>", "Escape"),
            ("<Control-a>", "ctrl+a"), ("<Control-c>", "ctrl+c"),
        ):
            self.root.bind_all(seq, lambda _e, n=name: self.key(n))

        self.entry.bind("<KeyRelease>", lambda _e: self.write())
        self.write()
        self.root.after(200, self.tick)

    def clicked(self) -> None:
        self.clicks += 1
        self.label.config(text=f"clicked {self.clicks}")
        self.write()

    def key(self, name: str) -> None:
        self.keys.append(name)
        self.write()

    def rect(self, widget: tk.Widget) -> dict[str, int]:
        """Where the widget actually is on screen, as the widget itself sees it."""
        return {
            "x": widget.winfo_rootx(),
            "y": widget.winfo_rooty(),
            "width": widget.winfo_width(),
            "height": widget.winfo_height(),
        }

    def write(self) -> None:
        payload = json.dumps({
            "entry": self.entry.get(),
            "clicks": self.clicks,
            "keys": self.keys,
            "label": self.label.cget("text"),
            "title": self.root.title(),
            "window_id": self.root.winfo_id(),
            "pid": os.getpid(),
            "focus": self.focus_name(),
            "window": self.rect(self.root),
            "confirm": self.rect(self.button),
            "field": self.rect(self.entry),
        })
        # Written by rename, because the reader is a test polling this file several
        # times a second while this loop rewrites it. A plain write truncates first, so
        # a read landing in that gap gets half a JSON document -- an exception in the
        # test, blamed on whatever it happened to be doing at the time.
        scratch = STATE.with_suffix(".tmp")
        scratch.write_text(payload, encoding="utf-8")
        os.replace(scratch, STATE)

    def focus_name(self) -> str:
        """Which widget Tk would give a keystroke to, by its ``name=``.

        Published because it is the precondition every typing test depends on and the
        only one that was invisible. ``focus_force()`` runs before the window is mapped,
        and if the window manager's focus arrives afterwards Tk can leave the keyboard
        focus on the toplevel: the text then goes nowhere, the tool still reports
        correct delivery to the right window, and the test fails 15 seconds later
        looking like a broken keyboard. With this in the state file the fixture waits
        for the entry to hold focus instead of assuming it.
        """
        widget = self.root.focus_get()
        return widget.winfo_name() if widget is not None else ""

    def tick(self) -> None:
        # Re-assert the entry's focus rather than trusting the one call at startup.
        # Tk's keyboard focus and the window manager's idea of the active window settle
        # independently, and only the second one is waited on outside; this closes the
        # gap from the inside, where it is cheap and certain.
        if self.root.focus_get() is not self.entry:
            self.entry.focus_force()
        self.write()
        if STATE.with_suffix(".quit").exists():
            self.root.destroy()
            return
        self.root.after(200, self.tick)


Probe().root.mainloop()

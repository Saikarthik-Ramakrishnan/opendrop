"""
OpenDrop: an open source AirDrop implementation
Copyright (C) 2026  Saikarthik Ramakrishnan

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import logging
import platform
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

ANSWER_TIMEOUT = 60  # seconds before an unanswered request counts as declined
_prompt_lock = threading.Lock()


def clean(text, limit=64):
    """
    Make sender-controlled text safe to show: no control characters (which
    could fake extra lines in a dialog) and a bounded length
    """
    text = "".join(c for c in str(text) if c.isprintable())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_size(size):
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000


def describe(ask):
    """
    Summarise an /Ask request, e.g. 'IMG_2584.JPG (895.6 KB)' or '3 files (2.1 MB)'
    """
    files = ask.get("Files") or []
    if not files:
        return "a link" if ask.get("Items") else "something"
    total = sum(f.get("FileSize", 0) for f in files if isinstance(f, dict))
    size = f" ({format_size(total)})" if total else ""
    if len(files) == 1:
        return clean(files[0].get("FileName", "a file")) + size
    return f"{len(files)} files{size}"


def ask_user(ask):
    """
    Ask whether to accept an incoming transfer. Returns True to accept.
    """
    sender = clean(ask.get("SenderComputerName") or "Someone nearby")
    question = f"{sender} wants to send you {describe(ask)}."
    with _prompt_lock:  # one question at a time
        if platform.system() == "Darwin":
            return _ask_dialog(question)
        return _ask_terminal(question)


def _ask_dialog(question):
    # The question goes in as an argument, never into the script source,
    # because it contains text chosen by the sender
    script = (
        "on run argv\n"
        'display dialog (item 1 of argv) with title "PigeonDrop" '
        'buttons {"Decline", "Accept"} default button "Accept" '
        f"giving up after {ANSWER_TIMEOUT}\n"
        "end run"
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script, question],
            capture_output=True,
            text=True,
            timeout=ANSWER_TIMEOUT + 10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning(f"Could not show the accept dialog: {e}")
        return False
    return "button returned:Accept" in result.stdout and "gave up:true" not in (
        result.stdout
    )


def _ask_terminal(question):
    if not sys.stdin.isatty():
        logger.warning(f"{question} Declined: no terminal to ask in")
        return False
    try:
        answer = input(f"{question} Accept? [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def notify(message):
    """
    Show a desktop notification where supported; always log the message
    """
    logger.info(message)
    if platform.system() != "Darwin":
        return
    script = (
        "on run argv\n"
        'display notification (item 1 of argv) with title "PigeonDrop"\n'
        "end run"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, message],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

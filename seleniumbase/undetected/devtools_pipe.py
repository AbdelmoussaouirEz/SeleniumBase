"""Load unpacked extensions into Chrome over the DevTools pipe.

Google Chrome 142+ ignores "--load-extension", so UC Mode launches Chrome
with "--remote-debugging-pipe" and loads the extensions with the CDP
command "Extensions.loadUnpacked" (which needs the pipe, not the port).
On POSIX, Chrome reads commands from fd 3 and writes replies to fd 4.
On Windows, Chrome uses the handles from "--remote-debugging-io-pipes".
Chrome exits when the pipe closes, so it stays open until quit().
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import warnings
from contextlib import suppress

IS_WINDOWS = "win32" in sys.platform
LOAD_TIMEOUT = 15  # Chrome might still be starting up


class DevToolsPipe:
    def __init__(self, write_fd, read_fd):
        self.__write_fd = write_fd
        self.__read_fd = read_fd  # (Owned by the reader thread)
        self.__last_id = 0
        self.__waiting = set()
        self.__replies = {}
        self.__closed = False
        self.__condition = threading.Condition()
        threading.Thread(target=self.__read_replies, daemon=True).start()

    def __read_replies(self):
        """Read until Chrome closes the pipe. Messages that nobody is
        waiting for are dropped, so nothing builds up unread."""
        buffer = b""
        try:
            while True:
                chunk = os.read(self.__read_fd, 65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\0" in buffer:
                    raw_message, buffer = buffer.split(b"\0", 1)
                    with suppress(ValueError):
                        message = json.loads(raw_message)
                        with self.__condition:
                            if message.get("id") in self.__waiting:
                                self.__replies[message["id"]] = message
                                self.__condition.notify_all()
        except OSError:
            pass
        finally:
            with suppress(OSError):
                os.close(self.__read_fd)
            with self.__condition:
                self.__closed = True
                self.__condition.notify_all()

    def send(self, method, params=None, timeout=LOAD_TIMEOUT):
        """Send a CDP command. Return its reply, or raise an Exception
        if the pipe closes or there's no reply before the timeout."""
        with self.__condition:
            if self.__closed:
                raise Exception("The DevTools pipe is closed")
            self.__last_id += 1
            message_id = self.__last_id
            self.__waiting.add(message_id)
        message = {"id": message_id, "method": method, "params": params or {}}
        data = json.dumps(message).encode("utf-8") + b"\0"
        try:
            while data:
                data = data[os.write(self.__write_fd, data):]
            deadline = time.monotonic() + timeout
            with self.__condition:
                while message_id not in self.__replies:
                    remaining = deadline - time.monotonic()
                    if self.__closed:
                        raise Exception("The DevTools pipe was closed")
                    elif remaining <= 0:
                        raise Exception("No reply to %s" % method)
                    self.__condition.wait(remaining)
                return self.__replies.pop(message_id)
        finally:
            with self.__condition:
                self.__waiting.discard(message_id)

    def load_extensions(self, extension_dirs):
        """Load unpacked extensions. Warn about the ones that fail."""
        for index, extension_dir in enumerate(extension_dirs):
            try:
                reply = self.send(
                    "Extensions.loadUnpacked", {"path": extension_dir}
                )
            except Exception as e:
                # The pipe isn't usable, so the rest can't load either.
                for failed_dir in extension_dirs[index:]:
                    warn_extension_not_loaded(failed_dir, str(e))
                return
            if "error" in reply:
                error = reply["error"].get("message", reply["error"])
                warn_extension_not_loaded(extension_dir, str(error))

    def close(self):
        """Close the pipe. (Chrome exits when its DevTools pipe closes.)"""
        with suppress(OSError):
            os.close(self.__write_fd)


def warn_extension_not_loaded(extension_dir, reason):
    message = (
        '\nWARNING: UC Mode could not load the extension at "%s" over the '
        'DevTools pipe (%s). Google Chrome 142+ ignores "--load-extension", '
        "so that extension (and proxy auth, if it's the proxy extension) "
        "won't work outside of CDP Mode." % (extension_dir, reason)
    )
    warnings.warn(message, category=Warning, stacklevel=2)


def _move_fd_up(fd, lowest=10):
    """Move fd up, so that the dup2 onto fds 0-4 can't collide with it."""
    import fcntl

    new_fd = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, lowest)
    os.close(fd)
    return new_fd


def _spawn_posix(command):
    """Start Chrome with stdio on /dev/null, and the pipe on fds 3 and 4.
    (posix_spawn passes the fds directly, without running Python code
    in the child, which isn't safe when other threads are running.)"""
    command_read, command_write = (_move_fd_up(fd) for fd in os.pipe())
    reply_read, reply_write = (_move_fd_up(fd) for fd in os.pipe())
    devnull = _move_fd_up(os.open(os.devnull, os.O_RDWR))
    # Like subprocess.Popen(restore_signals=True)
    signals = [
        getattr(signal, name) for name in ("SIGPIPE", "SIGXFSZ")
        if hasattr(signal, name)
    ]
    try:
        pid = os.posix_spawnp(
            command[0],
            command,
            os.environ,
            file_actions=[
                (os.POSIX_SPAWN_DUP2, devnull, 0),
                (os.POSIX_SPAWN_DUP2, devnull, 1),
                (os.POSIX_SPAWN_DUP2, devnull, 2),
                (os.POSIX_SPAWN_DUP2, command_read, 3),
                (os.POSIX_SPAWN_DUP2, reply_write, 4),
            ],
            setsigdef=signals,
        )
    except Exception:
        os.close(command_write)
        os.close(reply_read)
        raise
    finally:
        for fd in (devnull, command_read, reply_write):
            os.close(fd)
    return pid, None, DevToolsPipe(command_write, reply_read)


def _spawn_windows(command, creationflags):
    """Start Chrome with the pipe handles given in the command line."""
    import msvcrt

    command_read, command_write = os.pipe()
    reply_read, reply_write = os.pipe()
    handle_in = msvcrt.get_osfhandle(command_read)
    handle_out = msvcrt.get_osfhandle(reply_write)
    os.set_handle_inheritable(handle_in, True)
    os.set_handle_inheritable(handle_out, True)
    command.insert(
        1, "--remote-debugging-io-pipes=%d,%d" % (handle_in, handle_out)
    )
    startupinfo = subprocess.STARTUPINFO()
    # Only these two handles are inherited (close_fds=True is required)
    startupinfo.lpAttributeList = {"handle_list": [handle_in, handle_out]}
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
    except Exception:
        os.close(command_write)
        os.close(reply_read)
        raise
    finally:
        os.close(command_read)
        os.close(reply_write)
    return process.pid, process, DevToolsPipe(command_write, reply_read)


def launch_chrome(binary_location, arguments, creationflags=0):
    """Start Chrome with a DevTools pipe (in addition to its port).
    Returns (pid, process, pipe). (process is None on POSIX.)"""
    command = [binary_location, "--remote-debugging-pipe"]
    if "--enable-unsafe-extension-debugging" not in arguments:
        command.append("--enable-unsafe-extension-debugging")
    command.extend(arguments)
    if IS_WINDOWS:
        return _spawn_windows(command, creationflags)
    return _spawn_posix(command)

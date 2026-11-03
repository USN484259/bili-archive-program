#!/usr/bin/env python3

# This file is mostly designed and implemented by opencode-deepseek-v4.1-flash

# Watch the "live_monitor" messages broadcast by monitor.py and, for every
# recording whose path matches one of the --match keywords, feed the growing
# <recording>/danmaku.ndjson into a tools/get_sc_bvid.py child process, so the
# super-chat list of that recording is written to a spreadsheet in --out-dir.
#
# Usage:
#   tools/sc_bvid_watch.py --msg-addr /srv/http/tmp/messaging.socket \
#       -o /srv/http/html/bili-arch/sc --match <keyword> -f xlsx -- \
#       -u /path/to/credential.txt --target-uid 12345
#
# Everything after "--" is forwarded verbatim to get_sc_bvid.py. This script
# appends "-f <format>" and "-o <out-dir>/<recording name>.<format>" itself.
#
# The child is only launched after the danmaku file appears, and it is stopped
# by closing its stdin (EOF) when the matching "record-stopped" message
# arrives, so it can flush and write its output before exiting. SIGINT/SIGTERM
# do the same before this script exits. Only one child is tracked at a time;
# recordings that start while another one is running are ignored.
#
# Like the other tools, run it from the project root.

import os
import sys
sys.path[0] = os.getcwd()

import select
import signal
import logging
import argparse
import subprocess
from contextlib import suppress

import constants
import fops
from messaging import MessagingClient
from utils import logger_init

# constants

POLL_TIMEOUT_MS = 500		# poll timeout, danmaku pump / file-wait interval
DANMAKU_FILE = "danmaku.ndjson"
CHILD_NAME = "get_sc_bvid.py"
FORMAT_EXT = {"csv": "csv", "xlsx": "xlsx"}

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TOOL_DIR)
CHILD_PATH = os.path.join(TOOL_DIR, CHILD_NAME)

# static objects

logger = logging.getLogger("bili_arch.sc_bvid_watch")


# classes

class ScBvidWatcher:
	def __init__(self, out_dir, out_format, matches, child_args, /, msg_addr = None):
		self.out_dir = os.path.abspath(out_dir)
		self.out_format = out_format
		self.matches = tuple(matches)
		self.child_args = list(child_args)
		self.msg_client = MessagingClient(msg_addr, reconnect = 1)

		# the recording currently tracked; only one at a time
		self.state = "idle"		# idle | waiting | running
		self.rec_path = None
		self.danmaku = None		# open danmaku.ndjson, followed like tail -f
		self.child = None
		self.child_stdin = None
		self.pending = b""		# bytes not yet forwarded (partial line)
		self.quitting = False

		# self-pipe (POSIX) to wake the poll loop from a signal handler
		self._wake_r, self._wake_w = os.pipe()
		os.set_blocking(self._wake_r, False)
		os.set_blocking(self._wake_w, False)
		self._msg_fd = self.msg_client.fileno()
		self.poll = select.poll()
		self.poll.register(self._wake_r, select.POLLIN)
		# fileno() is stable for the client even across reconnects
		self.poll.register(self._msg_fd, select.POLLIN)


	# signal handling

	def on_signal(self, signum, frame):
		# runs on the main thread; only set the flag and wake the loop, the
		# child is flushed and stopped from run()/close()
		logger.debug("got signal %d", signum)
		self.quitting = True
		self._wake()


	def _wake(self):
		with suppress(OSError):
			os.write(self._wake_w, b"x")


	def handle_wakeup(self):
		with suppress(OSError):
			while os.read(self._wake_r, 0x1000):
				pass


	# messaging

	def handle_message(self):
		try:
			self.msg_client.wait(0)

			# drain pending messages
			while True:
				topic, info = self.msg_client.recv()
				logger.trace("%s: %s", topic, info)
				if not topic:
					break
				if not isinstance(info, dict):
					continue
				if topic == constants.topic.live_monitor:
					self.handle_monitor(info)
				else:
					logger.warning("unexpected topic %s", topic)
		except Exception:
			logger.exception("exception in handle_message")


	def handle_monitor(self, info):
		event = info.get("event")
		path = info.get("path")
		if not isinstance(path, str):
			return

		if event == "record-started":
			if self.state != "idle":
				logger.warning("still busy with %s, ignoring %s", self.rec_path, path)
				return
			if not self.match_path(path):
				logger.verbose("skip recording %s", path)
				return
			logger.info("recording started: %s", path)
			self.state = "waiting"
			self.rec_path = path

		elif event == "record-stopped":
			if self.state == "idle":
				return
			if os.path.normpath(path) != os.path.normpath(self.rec_path):
				logger.verbose("stopped %s is not the tracked recording %s", path, self.rec_path)
				return
			if self.state == "waiting":
				logger.notice("recording stopped before %s appeared: %s", DANMAKU_FILE, path)
				self.reset()
			else:
				logger.info("recording stopped: %s", path)
				self.stop_child()
				self.reset()


	def match_path(self, path):
		return (not self.matches) or any(keyword in path for keyword in self.matches)


	# child process

	def check_start(self):
		if self.state != "waiting":
			return
		danmaku_path = os.path.join(self.rec_path, DANMAKU_FILE)
		if not os.path.isfile(danmaku_path):
			return
		self.launch_child(danmaku_path)


	def make_output_path(self):
		name = os.path.basename(os.path.normpath(self.rec_path))
		ext = FORMAT_EXT.get(self.out_format, "csv")
		path = os.path.join(self.out_dir, "%s.%s" % (name, ext))
		# never overwrite a previous result (csv_writer opens with mode "x")
		seq = 1
		while os.path.exists(path):
			path = os.path.join(self.out_dir, "%s.%d.%s" % (name, seq, ext))
			seq += 1
		return path


	def launch_child(self, danmaku_path):
		if not os.access(CHILD_PATH, os.F_OK):
			logger.error("%s not found at %s", CHILD_NAME, CHILD_PATH)
			self.reset()
			return

		fops.mkdir(self.out_dir)
		out_path = self.make_output_path()
		cmd = [sys.executable, CHILD_PATH] + ["-f", self.out_format, "-o", out_path] + self.child_args
		logger.info("launching %s -> %s", CHILD_NAME, out_path)
		logger.verbose("command: %s", ' '.join(cmd))

		danmaku = None
		child = None
		try:
			danmaku = open(danmaku_path, mode = "rb")
			# stdout/stderr are inherited so the child's log cannot fill a pipe
			child = subprocess.Popen(cmd, stdin = subprocess.PIPE, cwd = PROJECT_ROOT)
		except Exception:
			logger.exception("cannot launch %s", CHILD_NAME)
			if danmaku is not None:
				with suppress(OSError):
					danmaku.close()
			if child is not None:
				with suppress(Exception):
					child.kill()
					child.wait()
			self.reset()
			return

		self.danmaku = danmaku
		self.child = child
		self.child_stdin = child.stdin
		self.pending = b""
		self.state = "running"


	def pump(self):
		# follow the file like "tail -f": a read at EOF returns b"", and a
		# later read picks up whatever was appended in the meantime
		if self.danmaku is None or self.child_stdin is None:
			return
		chunk = self.danmaku.read()
		if not chunk:
			return
		self.pending += chunk
		self.flush_pending()


	def flush_pending(self, final = False):
		if self.child_stdin is None:
			return
		if final:
			data, self.pending = self.pending, b""
		else:
			# forward only complete lines, keep the trailing partial one
			idx = self.pending.rfind(b"\n") + 1
			if not idx:
				return
			data, self.pending = self.pending[:idx], self.pending[idx:]
		if not data:
			return
		try:
			self.child_stdin.write(data)
			self.child_stdin.flush()
		except (BrokenPipeError, OSError) as e:
			logger.warning("cannot write to %s: %s", CHILD_NAME, str(e))
			self.close_child_input()


	def close_child_input(self):
		stdin = self.child_stdin
		if stdin is None and self.child is not None:
			stdin = self.child.stdin
		if stdin is not None:
			logger.verbose("closing %s stdin", CHILD_NAME)
			with suppress(OSError):
				stdin.close()
		self.child_stdin = None


	def stop_child(self):
		# flush the trailing partial line, then EOF so the child writes its
		# output and exits; wait for it so nothing is lost
		self.flush_pending(final = True)
		self.close_child_input()
		if self.child is not None:
			logger.info("waiting for %s %d to finish", CHILD_NAME, self.child.pid)
			rc = self.child.wait()
			logger.info("%s %d exited with %s", CHILD_NAME, self.child.pid, rc)
			self.child = None
		if self.danmaku is not None:
			with suppress(OSError):
				self.danmaku.close()
			self.danmaku = None
		self.pending = b""


	def reap_child(self):
		# catch a child that exited on its own (bad credential, bad args, ...)
		if self.child is None:
			return
		rc = self.child.poll()
		if rc is not None:
			logger.warning("%s exited unexpectedly with %s", CHILD_NAME, rc)
			self.reset()


	def reset(self):
		if self.danmaku is not None:
			with suppress(OSError):
				self.danmaku.close()
			self.danmaku = None
		self.pending = b""
		self.child = None
		self.child_stdin = None
		self.rec_path = None
		self.state = "idle"


	# main loop

	def run(self):
		logger.debug("setting signal handlers")
		signal.signal(signal.SIGINT, self.on_signal)
		signal.signal(signal.SIGTERM, self.on_signal)

		self.msg_client.subscribe(constants.topic.live_monitor)
		logger.info("output dir %s", self.out_dir)
		logger.info("watching recordings matching: %s", ' '.join(self.matches))

		while not self.quitting:
			for fd, ev in self.poll.poll(POLL_TIMEOUT_MS):
				if fd == self._wake_r:
					self.handle_wakeup()
				elif fd == self._msg_fd:
					self.handle_message()

			self.check_start()
			self.pump()
			self.reap_child()


	def close(self):
		logger.verbose("closing")
		self.quitting = True
		self.stop_child()

		with suppress(OSError):
			os.close(self._wake_r)
		self._wake_r = -1
		with suppress(OSError):
			os.close(self._wake_w)
		self._wake_w = -1

		self.msg_client.close()


	def __enter__(self):
		return self


	def __exit__(self, exc_type, exc_value, traceback):
		self.close()


# entrance

def parse_args():
	# everything after "--" goes to get_sc_bvid.py

	parser = argparse.ArgumentParser(description = "auto-run get_sc_bvid.py for new live recordings")
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-l", "--log")
	parser.add_argument("--msg-addr")
	parser.add_argument("-o", "--out-dir", required = True)
	parser.add_argument("-f", "--format", choices = ("csv", "xlsx"), default = "csv")
	parser.add_argument("--match", nargs = "*", metavar = "KEYWORD",
		help = "run for recordings whose path contains KEYWORD (repeatable)")
	parser.add_argument("child_args", nargs = "*")
	args = parser.parse_args()
	return args


def main(args):
	logger_init(args.verbose, args.log)
	logger.trace(args)

	with ScBvidWatcher(args.out_dir, args.format, args.match, args.child_args, msg_addr = args.msg_addr) as watcher:
		watcher.run()


if __name__ == "__main__":
	main(parse_args())

#!/usr/bin/env python3

# This file is written with the assistance of opencode-deepseek-v4-flash

# This daemon replaces the old implementation that relied on CPython/Linux
# tricks (inotify + flock(2) waiting interrupted by signals, ioprio_set via
# ctypes) to guess when a video finished downloading. That approach was fragile.
#
# Instead, this daemon subscribes to the messaging server and reacts to the
# exact "video" messages that video.py publishes after each download. The main
# thread listens on the messaging socket (and for SIGUSR1, which schedules a
# full video-folder walk), and hands each finished bvid over to a worker thread
# that updates the database, so slow SQLite/parsing work never blocks reception
# of further messages.
#
# A POSIX "self-pipe" is used to wake the main thread's poll() out of a signal
# handler; this is the portable way to interrupt select()/poll() without
# polling in a loop.


import os
import select
import signal
import logging
import threading
from queue import SimpleQueue, Empty as QueueEmpty
from contextlib import suppress

from messaging import MessagingClient
from video_database import VideoDatabaseManager
import constants

# constants
QUEUE_MAX_SIZE = 0x40

# static object

logger = logging.getLogger("bili_arch.database_daemon")


class DatabaseDaemon:
	def __init__(self, video_root, database, /, msg_addr = None, *, allow_dummy = True, watch_paths = None):
		self.video_root = video_root
		self.database = VideoDatabaseManager(video_root, database)
		self.msg_client = MessagingClient(msg_addr, allow_dummy = allow_dummy)
		self.queue = SimpleQueue()
		self.watch_paths = set()
		self.worker = None
		self.quitting = False
		self.needs_walk = False

		self.watch_paths.add(os.path.normpath(video_root))
		for path in (watch_paths or ()):
			self.watch_paths.add(os.path.normpath(path))

		# self-pipe (POSIX) to wake the main loop from a signal handler
		self._wake_r, self._wake_w = os.pipe()
		os.set_blocking(self._wake_r, False)
		os.set_blocking(self._wake_w, False)

		self.poll = select.poll()
		self.poll.register(self._wake_r, select.POLLIN)
		# fileno() is stable for the client even across reconnects, so it is
		# safe to register it once. For a dummy client it is never ready.
		self.poll.register(self.msg_client.fileno(), select.POLLIN)


	def on_signal(self, signum, frame):
		# runs on the main thread; keep it minimal and async-signal-friendly
		if signum == signal.SIGUSR1:
			self.needs_walk = True
		else:
			self.quitting = True
		self._wake()


	def _wake(self):
		with suppress(OSError):
			os.write(self._wake_w, b"x")


	def _schedule_task(self, task = "wakeup"):
		size = self.queue.qsize()
		if size >= QUEUE_MAX_SIZE:
			logger.warning("queue full %d, dropping %s", size, str(task))
		else:
			self.queue.put_nowait(task)


	def handle_event(self):
		# drain the wake-up self-pipe
		with suppress(OSError):
			while os.read(self._wake_r, 4096):
				pass

		if self.needs_walk:
			logger.info("scheduled video folder walk")
		if self.quitting:
			logger.info("quitting")

		self._schedule_task()

	def handle_message(self):
		try:
			self.msg_client.wait(0)

			# drain pending messages
			while True:
				topic, info = self.msg_client.recv(constants.topic.video)
				if not topic:
					break
				if not isinstance(info, dict):
					return
				path = info.get("path")
				bvid = info.get("bvid")
				if not (isinstance(path, str) and isinstance(bvid, str) and constants.bvid_pattern.fullmatch(bvid)):
					logger.warning("invalid video message: %s %s", str(path), str(bvid))
					return
				done = info.get("done")
				logger.debug("video %s, path %s, done %s", bvid, path, str(done))
				if not done or os.path.normpath(path) not in self.watch_paths:
					return
				logger.info("scheduling database update %s", bvid)
				self._schedule_task(bvid)
		except Exception:
			logger.exception("exception in handle_message")


	def worker_func(self):
		# change this thread's IO priority to IDLE so database work stays out
		# of the way of other IO
		try:
			import psutil
			proc = psutil.Process(threading.get_native_id())
			proc.ionice(psutil.IOPRIO_CLASS_IDLE)
		except Exception as e:
			logger.warning("cannot set IO priority: %s", str(e))

		while not self.quitting:
			item = self.queue.get()
			try:
				if self.quitting:
					break
				elif self.needs_walk:
					# running full walk and drop individual updates
					with suppress(QueueEmpty):
						while self.queue.get_nowait():
							pass
					try:
						self.database.walk()
					finally:
						self.needs_walk = False
				else:
					bvid = item
					logger.info("running database update %s", bvid)
					if self.database.update_video(bvid):
						self.database.update_video_size(bvid)
			except Exception as e:
				logger.exception("exception on updating %s", item)



	def close(self):
		self.quitting = True
		self._schedule_task()
		self._wake()
		if self.worker is not None:
			with suppress(Exception):
				self.worker.join()
			self.worker = None

		with suppress(OSError):
			os.close(self._wake_r)
		self._wake_r = -1

		with suppress(OSError):
			os.close(self._wake_w)
		self._wake_w = -1

		try:
			self.msg_client.close()
		finally:
			self.database.close()


	def __enter__(self):
		return self


	def __exit__(self, exc_type, exc_value, traceback):
		self.close()


	def run(self):
		signal.signal(signal.SIGUSR1, self.on_signal)
		signal.signal(signal.SIGTERM, self.on_signal)
		signal.signal(signal.SIGINT, self.on_signal)

		self.msg_client.subscribe(constants.topic.video)

		self.worker = threading.Thread(target = self.worker_func, daemon = True)
		self.worker.start()

		while not self.quitting:
			for ev, fd in self.poll.poll():
				if fd == self._wake_r:
					self.handle_event()
				else:
					self.handle_message()


def main(args):
	video_path = args.dir or runtime.subdir("video")
	with DatabaseDaemon(video_path, args.database, args.msg_addr, watch_paths = args.watch) as daemon:
		daemon.run()


if __name__ == "__main__":
	import runtime

	args = runtime.parse_args(("dir", "messaging"), (
		(("--watch", ), {"nargs": '*'}),
		(("database", ), {}),
	))

	main(args)

#!/usr/bin/env python3

# This daemon utilizes multiple tricks in CPython and Linux to work. Below are the details.
#
# 1. Waiting for flock(2) with timeout (see timed_thread_pool.py)
# The video.py would place flock(2) on the BV directory when working, and release when done.
# We depends on this behavior to detect when video.py has done by tryng to also place a flock(2) on it.
# The syscall would block, until video.py release the lock, and the syscall returns with the lock held.
# However to prevent possible deadlock, the waiting should have a timeout. If we don't want polling in a loop,
# the only practical way is using a signal to interrupt the syscall and make it return EINTR.
# The problem is, how to do this in Python. signal.pthread_kill can be used to send signal to a thread,
# but the the signal handlers are always called on the main thread.
# The implementation is a thread pool who would send a signal to the running task on worker thread on timeout.
# see timed_thread_pool.py. Below are the sequence when a signal is sent to the worker thread.
#	0. the thread is running the task, possibly blocking in a syscall. Now the thread gets a signal.
#	1. The CPython signal handler is called on that thread, CPython would record the incoming signal
#	   and prepare to call its Python handler when back to Python code. The signal handler returns.
#	2. the syscall got interrupted, returns EINTR, results are collected and goes back to Python code.
#	3. When backing to Python code, the pending Python signal handler is called on the main thread.
#	4. If the Python signal handler does not raise and returns, Python code continues to normal flow.
#	5. In the thread being sent signal, the Python code got the result that syscall is interrupted.
#
# 2. Problem with fcntl.flock
# Python does provide the flock(2) wrapper as fcntl.flock(). However this wrapper is not usable in this scenaio.
# According to PEP-0475, Python would auto retry the syscall when interrupted, instead of returning EINTR.
# The CPython implementation of fcntl.flock() is at Modules/fcntlmodule.c, function fcntl_flock_impl.
# The condition is "while (ret == -1 && errno == EINTR && !(async_err = PyErr_CheckSignals()))"
# So in current implementation the flock(2) is auto retried unless the Python signal handler raises.
# However, since the Python signal handlers are always called in the main thread, raising in the signal handler
# would be injected into the main thread, instead of the thread being sent signal. This could cause the main
# thread be interrupted and raise at any point of its code flow, which is hard to handle.
# The solution is not raising in signal handlers, and to avoid the logic above. We are not using fcntl.flock(),
# instead using ctypes to grab the flock(2) syscall directly from libc, call it and prepare to handle EINTR.
#
# 3. IO priority
# On Linux, ionice(1) can start a process using altered IO priority, the underlying syscall is ioprio_set(2).
# What we want is setting the IO priority of the database worker thread to IOPRIO_CLASS_IDLE, while leaving
# other threads unchanged. So we cannot start the daemon using ionice(1) since it affects the whole process.
# According to the ioprio_set(2) man page, when IOPRIO_WHO_PROCESS is set in the "which" parameter,
# "who is a process ID or thread ID identifying a single process or thread". And it seems that for the CFQ
# IO scheduler, it is treated as thread ID. So calling ioprio_set(2) with tid could achieve this.
# In Python there is "psutil" package. Its Process.ionice() method could "set the IO priority of a process".
# On Linux the implementation is just a simple wrapper around ioprio_set(2) that passes pid in.
# So we just create a Process instance using tid of the database thread as "pid", and call its ionice() method.


import os
import errno
import ctypes
import ctypes.util
import struct
import select
import signal
import logging
import threading

from errno import EINTR, EWOULDBLOCK
from fcntl import LOCK_EX, LOCK_SH, LOCK_UN, LOCK_NB
from contextlib import suppress
from collections import OrderedDict

from video_database import VideoDatabaseManager
from simple_inotify import *
from timed_thread_pool import TimedThreadPool

# constants

import constants


# static object

logger = logging.getLogger("bili_arch.database_daemon")


# flock syscall

libc_path = ctypes.util.find_library('c') or 'libc.so.6'
libc_instance = ctypes.CDLL(libc_path, use_errno = True)

def check_result(result):
	if result < 0:
		errno = ctypes.get_errno()
		if errno == EINTR:
			raise InterruptedError()
		elif errno == EWOULDBLOCK:
			raise BlockingIOError()

		err_str = ""
		with suppress(ValueError):
			err_str = os.strerror(errno)

		raise OSError(errno, err_str)
	return result

flock = libc_instance.flock
flock.argtypes = (ctypes.c_int, ctypes.c_int)
flock.restype = check_result

# classes

class Poll:
	def __init__(self):
		self.poll_obj = select.poll()
		self.map = {}
		self.wakeup_fd = os.eventfd(0, os.EFD_NONBLOCK | os.EFD_CLOEXEC)
		self.poll_obj.register(self.wakeup_fd, select.POLLIN)

	def close(self):
		# currently just wakeup
		self.wakeup()

	def wakeup(self):
		with suppress(OSError):
			os.eventfd_write(self.wakeup_fd, 1)

	def register(self, fd, func, *, mask = select.POLLIN):
		self.poll_obj.register(fd, mask)
		self.map[fd] = func
		with suppress(OSError):
			os.eventfd_write(self.wakeup_fd, 1)

	def unregister(self, fd):
		with suppress(KeyError):
			del self.map[fd]
		with suppress(KeyError):
			self.poll_obj.unregister(fd)

	def poll(self, timeout = None):
		results = self.poll_obj.poll(timeout and int(timeout * 1000) or timeout)
		for fd, ev in results:
			if ev & select.POLLNVAL:
				with suppress(KeyError):
					self.poll_obj.unregister(fd)
				continue
			if fd == self.wakeup_fd:
				with suppress(OSError):
					os.eventfd_read(self.wakeup_fd)
				continue
			func = self.map.get(fd)
			if not func:
				logger.warning("no handler for %d %x", fd, ev)
				continue
			try:
				func(ev)
			except Exception:
				logger.exception("exception in poll callback")


class CacheMonitor:
	def __init__(self, manager):
		self.manager = manager
		self.wd_map = {}
		self.bv_map = {}

	def __del__(self):
		self.close()

	def register_wd(self, rec):
		wd = self.manager.register(rec["bvid"])
		assert(wd not in self.wd_map)
		rec["wd"] = wd
		self.wd_map[wd] = rec

	def del_record(self, rec):
		wd = rec["wd"]
		fd = rec["fd"]

		logger.debug("closing %s %s", rec["bvid"], (fd is not None) and str(fd) or "")
		if fd is not None:
			with suppress(OSError):
				os.close(fd)

		if wd is not None:
			try:
				self.manager.unregister(wd)
			except Exception as e:
				logger.error("cannot unregister wd %d: %s", wd, str(e))

			self.wd_map.pop(wd, None)

		self.bv_map.pop(rec["bvid"], None)

	def close(self):
		for wd in self.wd_map.keys():
			with suppress(Exception):
				self.manager.unregister(wd)
		self.wd_map.clear()

		for rec in self.bv_map.values():
			fd = rec["fd"]
			if fd is not None:
				with suppress(Exception):
					os.close(fd)
		self.bv_map.clear()


	def handle_bvid(self, bvid, ev):
		if ev & (IN_OPEN | IN_CREATE):
			ref = 1
		elif ev & (IN_CLOSE | IN_DELETE | IN_MOVED_FROM):
			ref = -1
		else:
			return

		rec = self.bv_map.get(bvid)
		if not rec:
			if ref > 0:
				rec = {
					"bvid": bvid,
					"ref": 0,
					"wd": None,
					"fd": None
				}
				self.bv_map[bvid] = rec
			else:
				return
		assert(rec["bvid"] == bvid)
		logger.debug("%s: %d %+d", bvid, rec["ref"], ref)
		rec["ref"] = max(rec["ref"] + ref, 0)

		if ref < 0 and rec["ref"] <= 0 and rec["fd"] is None:
			self.del_record(rec)
			return

		if ref > 0 and rec["fd"] is None and rec["wd"] is None:
			self.register_wd(rec)
			return

	def handle_wd(self, wd, ev):
		if ev & (IN_CREATE | IN_MODIFY | IN_CLOSE_WRITE | IN_MOVED_TO):
			pass
		else:
			return

		rec = self.wd_map.get(wd)
		if (not rec) or rec["fd"] is not None:
			return

		bvid = rec["bvid"]
		video_path = os.path.join(self.manager.video_root, bvid)
		fd = os.open(video_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
		logger.info("monitoring %s", bvid)
		logger.debug("%s %d", video_path, fd)
		try:
			flock(fd, LOCK_SH | LOCK_NB)
			os.close(fd)
			return
		except BlockingIOError:
			rec["fd"] = fd
			self.manager.schedule(bvid, fd)
			# self.manager.unregister(wd)
			# rec["wd"] = None
			# self.wd_map.pop(wd, None)
			return
		except Exception as e:
			logger.error("exception on checking %s: %s", bvid, e)

	def handle_complete(self, bvid, result):
		rec = self.bv_map.get(bvid)
		assert(rec and rec["fd"] is not None)
		with suppress(OSError):
			os.close(rec["fd"])
		rec["fd"] = None

		if rec["ref"] <= 0:
			self.del_record(rec)
			return

		# if not result and rec["wd"] is None:
		# 	self.register_wd(rec)


class CacheManager:
	def __init__(self, video_root, /, database = None, *, num_threads = 64, timeout = 300):
		self.video_root = video_root
		self.monitor = CacheMonitor(self)
		self.monitor_lock = threading.Lock()
		self.update_queue = OrderedDict()
		self.update_cv = threading.Condition()
		self.database_task = None
		self.quit = False
		self.poll = Poll()
		self.database = VideoDatabaseManager(video_root, database)
		self.observer = Inotify(IN_NONBLOCK | IN_CLOEXEC)
		self.root_wd = self.observer.add_watch(video_root, IN_ONLYDIR | IN_ALL_EVENTS)
		self.wait_pool = TimedThreadPool(signal.SIGALRM, self.poll, num_threads)
		self.timeout = timeout
		self.needs_walk = False
		self.poll.register(self.observer.fileno(), self.handle_update)
		logger.info("video %s, db %s, threads %d, timeout %d", video_root, database or "<none>", num_threads, timeout)


	def close(self):
		self.quit = True
		# stop poll
		self.poll.close()

		# close monitor
		with self.monitor_lock:
			self.monitor.close()

		# close observer
		self.observer.close()

		# stop wait_pool
		self.wait_pool.close()

		# stop update thread
		with self.update_cv:
			self.update_cv.notify_all()

		# close database
		self.database.close()


	def register(self, bvid):
		video_path = os.path.join(self.video_root, bvid)
		return self.observer.add_watch(video_path)


	def unregister(self, wd):
		self.observer.rm_watch(wd)


	def schedule(self, bvid, fd):
		self.wait_pool.submit(self.wait_unlock_func, bvid, fd, timeout = self.timeout, nowait = True)


	def wait_unlock_func(self, bvid, fd):
		logger.info("waiting on %s", bvid)
		result = False
		try:
			flock(fd, LOCK_SH)
			result = True
		except InterruptedError:
			# timeout
			logger.warning("timeout waiting for %s", bvid)
			return
		except Exception as e:
			logger.error("exception waiting for %s: %s", bvid, e)
			return
		finally:
			with self.monitor_lock:
				self.monitor.handle_complete(bvid, result)

		# video folder successfully unlocked, schedule the update
		logger.info("updating %s", bvid)
		with self.update_cv:
			self.update_queue[bvid] = True
			self.update_queue.move_to_end(bvid, last = True)
			self.update_cv.notify()


	def handle_update(self, *args):
		events = self.observer.read()
		for ev in events:
			try:
				if ev.wd == self.root_wd:
					if ev.mask & IN_ISDIR and constants.bvid_pattern.fullmatch(ev.name):
						bvid = ev.name
						# logger.debug("%s: %x", bvid, ev.mask)
						with self.monitor_lock:
							self.monitor.handle_bvid(bvid, ev.mask)

				else:
					with self.monitor_lock:
						self.monitor.handle_wd(ev.wd, ev.mask)

			except Exception as e:
				logger.exception("exception on event %s 0x%x: %s", ev.name, ev.mask, e)


	def database_func(self):
		try:
			# Try to change IO priority to IDLE
			# Maybe an abuse of psutil, since it just call ioprio_set(2)
			# with the provided "pid". If passed in current tid, this
			# would actually sets the IO priority of the current thread
			import psutil
			tid = threading.get_native_id()
			th = psutil.Process(tid)
			th.ionice(psutil.IOPRIO_CLASS_IDLE)
		except Exception as e:
			logger.warning("cannot set IO priority: %s", str(e))

		with self.update_cv:
			while not self.quit:
				try:
					bvid = self.update_queue.popitem(last = False)[0]
				except KeyError:
					self.update_cv.wait()
					continue
				try:
					if bvid == "walk":
						self.database.walk()
					elif self.database.update_video(bvid):
						self.database.update_video_size(bvid)
				except Exception as e:
					logger.exception("exception on updating %s: %s", bvid, e)


	def __enter__(self):
		return self


	def __exit__(self, exc_type, exc_value, traceback):
		self.close()


	def run(self):
		th = threading.Thread(target = self.database_func)
		self.database_task = th
		th.start()
		while not self.quit:
			self.poll.poll()
			if self.needs_walk:
				self.needs_walk = False
				with self.update_cv:
					logger.info("scheduled walking")
					self.update_queue.clear()
					self.update_queue["walk"] = True
					self.update_cv.notify()


	def walk(self):
		self.needs_walk = True
		self.poll.wakeup()


def main(args):
	video_path = args.dir or runtime.subdir("video")

	with CacheManager(video_path, args.database, timeout = args.timeout) as cache_manager:
		def sig_walk(signum, frame):
			cache_manager.walk()

		signal.signal(signal.SIGUSR1, sig_walk)

		cache_manager.run()


if __name__ == "__main__":
	import runtime

	args = runtime.parse_args(("dir", ), (
		(("database", ), {}),
		(("-t", "--timeout"), {"type": int, "default": 300}),
	))

	main(args)

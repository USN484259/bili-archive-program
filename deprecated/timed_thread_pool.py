#!/usr/bin/env python3

import os
import time
import signal
import select
import logging
import warnings
import threading
from contextlib import suppress
from collections import deque
from functools import partial

# static objects

logger = logging.getLogger("bili_arch.timed_thread_pool")

def empty_func(*args):
	pass

# classes

class ThreadPoolCongestionError(RuntimeError):
	pass

class TimedThreadPool:
	# constructor
	def __init__(self, signum, poll, /, max_threads = None):
		self.poll = poll
		self.signum = signum
		self.max_threads = max_threads
		self.all_threads = {}
		self.idle_threads = deque()
		# deque itself is atomic, but we need cv for waiting
		self.cv = threading.Condition()
		self.done_ev = threading.Event()
		self.done_ev.set()
		self.quit = False

		orig_handler = signal.getsignal(signum)
		if orig_handler is None or orig_handler == signal.SIG_IGN or orig_handler == signal.SIG_DFL:
			signal.signal(signum, empty_func)
		elif orig_handler != empty_func:
			warnings.warn("TimedThreadPool: signal %d already has handler, skip registration", RuntimeWarning)

	# private methods

	def on_timeout(self, worker, *args):
		tid = worker["thread"].ident
		print("timeout ", tid)
		with suppress(BlockingIOError):
			os.read(worker["timer"], 8)
		signal.pthread_kill(tid, self.signum)

	def worker_thread(self, worker):
		tid = threading.get_ident()
		while not worker["quit"]:
			worker["event"].wait()
			worker["event"].clear()
			payload = worker["payload"]
			if payload is None:
				continue
			worker["payload"] = None

			timeout = payload[0]
			print("thread ", tid, ", timeout ", timeout)
			if timeout:
				os.timerfd_settime(worker["timer"], initial = timeout, interval = timeout)
			# print(os.timerfd_gettime_ns(worker["timer"]))
			try:
				payload[1](*payload[2])
			except Exception as e:
				print(e)
			finally:
				os.timerfd_settime(worker["timer"], initial = 0)
				with self.cv:
					self.idle_threads.append(worker)
					print("finish", tid, len(self.idle_threads))
					if len(self.all_threads) == len(self.idle_threads):
						self.done_ev.set()
					self.cv.notify()

	def start_thread(self):
		timer_fd = os.timerfd_create(time.CLOCK_MONOTONIC)
		worker = {
			"pool": self,
			"event": threading.Event(),
			"timer": timer_fd,
			"thread": None,
			"quit": False,
			"payload": None
		}
		thread = threading.Thread(target = self.worker_thread, args = (worker, ))
		worker["thread"] = thread
		self.poll.register(timer_fd, partial(self.on_timeout, worker))
		thread.start()
		tid = thread.ident
		self.all_threads[tid] = worker
		self.idle_threads.appendleft(worker)

		print("created thread ", tid, ", total ", len(self.all_threads))

	def wait_for_worker(self, nowait):
		# assert(self.cv.locked())
		if self.max_threads and len(self.all_threads) >= self.max_threads:
			if nowait:
				raise ThreadPoolCongestionError("thread pool congestion %d", self.max_threads)
			else:
				print("waiting for worker thread")
				self.cv.wait()
		else:
			print("creating worker thread")
			self.start_thread()

	# public methods

	def submit(self, func, *args, timeout = None, nowait = False):
		if timeout is None or isinstance(timeout, (int, float)):
			pass
		else:
			raise ValueError("%s is not a timeout value" % timeout)
		if not callable(func):
			raise ValueError("%s is not a callable" % func)

		worker = None
		with self.cv:
			while worker is None:
				if self.quit:
					raise RuntimeError("thread pool already closed")
				try:
					worker = self.idle_threads.popleft()
					break
				except IndexError:
					pass
				self.wait_for_worker(nowait)

			self.done_ev.clear()

		assert(worker["payload"] is None)
		worker["payload"] = (timeout, func, args)
		worker["event"].set()

	def wait(self, timeout = None):
		return self.done_ev.wait(timeout)

	def shrink(self, count):
		while len(self.all_threads) > count:
			with self.cv:
				try:
					worker = self.idle_threads.popleft()
				except IndexError:
					break

				assert(worker["payload"] is None)
				tid = worker["thread"].ident
				del self.all_threads[tid]

			worker["quit"] = True
			worker["event"].set()
			worker["thread"].join()
			timer_fd = worker["timer"]
			self.poll.unregister(timer_fd)
			os.close(timer_fd)

		return len(self.all_threads)

	def close(self):
		self.quit = True
		while len(self.all_threads) > 0:
			self.wait()
			self.shrink(0)

	# context manager
	def __enter__(self):
		return self

	def __exit__(self, exc_type, exc_value, traceback):
		self.close()

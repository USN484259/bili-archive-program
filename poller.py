#!/usr/bin/env python3

# This file is written with the assistance of opencode-deepseek-v4-flash

import os
import time
import logging
import select
from contextlib import suppress

# constants

ns_in_ms = 1000*1000
ns_in_sec = 1000 * 1000 * 1000

# static objects

logger = logging.getLogger("bili-arch.poller")


# timerfd API

timerfd_create = None
timerfd_settime_ns = None
TFD_CLOEXEC = getattr(os, "TFD_CLOEXEC", 0o02000000)
TFD_NONBLOCK = getattr(os, "TFD_NONBLOCK", 0o00004000)

if hasattr(os, "timerfd_create"):
	timerfd_create = os.timerfd_create
	timerfd_settime_ns = os.timerfd_settime_ns

elif not hasattr(select, "kqueue"):	# only use timerfd if kqueue is not present
	# find out timerfd API for Python < 3.13 and OS that support it but not exposed by Python
	with suppress(Exception):
		import ctypes
		import ctypes.util
		import errno

		class timespec(ctypes.Structure):
			_fields_ = (("tv_sec", ctypes.c_time_t), ("tv_nsec", ctypes.c_int64))

		class itimerspec(ctypes.Structure):
			_fields_ = (("it_interval", timespec), ("it_value", timespec))

		c_itimerspec_p = ctypes.POINTER(itimerspec)

		def check_result(result):
			if result < 0:
				errno = ctypes.get_errno()
				err_str = ""
				with suppress(ValueError):
					err_str = os.strerror(errno)

				raise OSError(errno, err_str)
			return result

		libc_path = ctypes.util.find_library('c') or 'libc.so.6'
		libc_instance = ctypes.CDLL(libc_path, use_errno = True)

		raw_timerfd_create = libc_instance.timerfd_create
		raw_timerfd_create.argtypes = (ctypes.c_int, ctypes.c_int)
		raw_timerfd_create.restype = check_result
		raw_timerfd_settime = libc_instance.timerfd_settime
		raw_timerfd_settime.argtypes = (ctypes.c_int, ctypes.c_int, c_itimerspec_p, c_itimerspec_p)
		raw_timerfd_create.restype = check_result

		def timerfd_create(clockid, /, *, flags = 0):
			return raw_timerfd_create(clockid, flags)

		def timerfd_settime_ns(fd, /, *, flags = 0, initial = 0, interval = 0):
			if initial < 0:
				raise OSError(errno.EINVAL)

			old_ts = itimerspec()
			new_ts = itimerspec(timespec(interval // ns_in_sec, interval % ns_in_sec),
					timespec(initial // ns_in_sec, initial % ns_in_sec))
			raw_timerfd_settime(fd, flags, ctypes.byref(new_ts), ctypes.byref(old_ts))

			return (old_ts.it_value.tv_sec * ns_in_sec + old_ts.it_value.tv_nsec,
				old_ts.it_interval.tv_sec * ns_in_sec + old_ts.it_interval.tv_nsec)


# Dummy poller / timer

class DummyPoller:
	dummy = True
	warned = False
	def __init__(self, /, edge = False):
		logger.debug("creating %s %x", type(self).__name__, id(self))
		if not self.warned:
			logger.warning("using dummy Poller")
			type(self).warned = True

		self.poll = select.poll()
		self._pipe_r, self._pipe_w = os.pipe()
		logger.trace("pipe %d %d", self._pipe_r, self._pipe_w)
		self.poll.register(self._pipe_r, select.POLLIN)

	def close(self):
		logger.debug("closing %s %x", type(self).__name__, id(self))
		for fd in (self._pipe_r, self._pipe_w):
			if fd >= 0:
				with suppress(OSError):
					os.close(fd)
		self._pipe_r = -1
		self._pipe_w = -1

	def fileno(self):
		return self._pipe_r

	def register(self, fd):
		pass

	def rearm(self, fd):
		pass

	def unregister(self, fd):
		pass

	def poll(self, timeout = None):
		logger.trace("poll %s", timeout)
		self.poll.poll(timeout and int(timeout * 1000))
		return tuple()

	@staticmethod
	def parse_event(ev):
		pass


class DummyTimer:
	dummy = True
	warned = False
	def __init__(self, poller = None):
		logger.debug("creating %s %x", type(self).__name__, id(self))
		if not self.warned:
			logger.warning("using dummy Timer")
			type(self).warned = True

		self._pipe_r, self._pipe_w = os.pipe()


	def close(self):
		logger.debug("closing %s %x", type(self).__name__, id(self))
		if self._pipe_r is not None:
			with suppress(OSError):
				os.close(self._pipe_r)
			self._pipe_r = None

		if self._pipe_w is not None:
			with suppress(OSError):
				os.close(self._pipe_w)
			self._pipe_w = None


	def fileno(self):
		return self._pipe_r

	def settime(self, ms, repeat = False):
		pass

	def cancel(self):
		pass

	def check_event(self, fd, ev):
		return False

	def drain(self):
		pass


Poller = DummyPoller
Timer  = DummyTimer


# OS specific poll/timer implementation

if hasattr(select, "epoll"):
	class EpollPoller:
		dummy = False
		def __init__(self, /, edge = False):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			self.epoll = select.epoll()
			self.epoll_mask = select.EPOLLIN | select.EPOLLRDHUP | (edge and select.EPOLLONESHOT or 0)
			logger.trace("epoll fd %d, mask %x", self.epoll.fileno(), self.epoll_mask)

		def close(self):
			logger.debug("closing %s %x", type(self).__name__, id(self))
			try:
				if self.epoll is not None:
					self.epoll.close()
			finally:
				self.epoll = None

		def fileno(self):
			return self.epoll.fileno()

		def register(self, fd):
			logger.trace("epoll register %d", fd)
			self.epoll.register(fd, self.epoll_mask)

		def rearm(self, fd):
			logger.trace("epoll rearm %d", fd)
			self.epoll.modify(fd, self.epoll_mask)

		def unregister(self, fd):
			logger.trace("epoll unregister %d", fd)
			with suppress(KeyError, OSError):
				self.epoll.unregister(fd)

		def poll(self, timeout = None):
			logger.trace("epoll poll %s", timeout)
			events = self.epoll.poll(timeout)
			logger.trace(events)
			return events

		@staticmethod
		def parse_event(ev):
			if ev & select.EPOLLRDHUP:
				return "eof"
			if ev & select.EPOLLERR:
				return "err"


	Poller = EpollPoller


if timerfd_create and timerfd_settime_ns:
	class TimerfdTimer:
		dummy = False
		def __init__(self, poller = None):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			self.poller = poller
			self.timerfd = timerfd_create(time.CLOCK_MONOTONIC, flags = TFD_NONBLOCK | TFD_CLOEXEC)
			if self.poller is not None:
				self.poller.register(self.timerfd)
			logger.trace("timerfd %d, poller %s", self.timerfd, self.poller and self.poller.fileno())

		def close(self):
			logger.debug("closing %s %x", type(self).__name__, id(self))
			if self.timerfd is not None:
				with suppress(OSError):
					if self.poller is not None:
						self.poller.unregister(self.timerfd)
					os.close(self.timerfd)
				self.timerfd = None

		def fileno(self):
			return self.timerfd

		def settime(self, ms, repeat = False):
			logger.trace("timerfd settime %d", ms)
			timeval = ms * ns_in_ms
			timerfd_settime_ns(self.timerfd, initial = timeval, interval = repeat and timeval or 0)

		def cancel(self):
			logger.trace("timerfd cancel")
			timerfd_settime_ns(self.timerfd)

		def check_event(self, fd, ev):
			if fd != self.timerfd:
				return False
			else:
				logger.trace("timerfd timer")
				return True

		def drain(self):
			with suppress(OSError):
				os.read(self.timerfd, 8)


	Timer = TimerfdTimer


if hasattr(select, "kqueue"):
	class KqueuePoller:
		dummy = False
		def __init__(self, /, edge = False):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			self.kqueue = select.kqueue()
			self.kqueue_flags = select.KQ_EV_ADD | (edge and select.KQ_EV_CLEAR or 0)
			logger.trace("kqueue fd %d, flags %x", self.kqueue.fileno(), self.kqueue_flags)

		def close(self):
			logger.debug("closing %s %x", type(self).__name__, id(self))
			try:
				if self.kqueue is not None:
					self.kqueue.close()
			finally:
				self.kqueue = None

		def fileno(self):
			return self.kqueue.fileno()

		def register(self, fd):
			logger.trace("kqueue register %d", fd)
			self.kqueue.control([select.kevent(fd, filter = select.KQ_FILTER_READ, flags = self.kqueue_flags)], 0, 0)

		def rearm(self, fd):
			logger.trace("kqueue rearm %d", fd)
			pass

		def unregister(self, fd):
			logger.trace("kqueue unregister %d", fd)
			with suppress(OSError):
				self.kqueue.control([select.kevent(fd, filter = select.KQ_FILTER_READ, flags = select.KQ_EV_DELETE)], 0, 0)

		def poll(self, timeout = None):
			logger.trace("kqueue poll %s", timeout)
			events = self.kqueue.control(None, 0x10, timeout)
			logger.trace(events)
			return [(ev.ident, ev.flags) for ev in events if ev.filter in (select.KQ_FILTER_READ, select.KQ_FILTER_TIMER)]

		@staticmethod
		def parse_event(ev):
			if ev & select.KQ_EV_EOF:
				return "eof"
			if ev & select.KQ_EV_ERROR:
				return "err"


	class KqueueTimer:
		dummy = False
		timer_id = 0xCDCDCDCD
		def __init__(self, poller = None):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			if poller is not None:
				self.kqueue = poller.kqueue
				self.flags = poller.kqueue_flags
				self.close_kq = False
			else:
				self.kqueue = select.kqueue()
				self.flags = select.KQ_EV_ADD
				self.close_kq = True

		def close(self):
			logger.debug("closing %s %x", type(self).__name__, id(self))
			self.cancel()
			if self.close_kq and self.kqueue is not None:
				self.kqueue.close()
				self.kqueue = None

		def fileno(self):
			if self.close_kq:
				return self.kqueue.fileno()

		def settime(self, ms, repeat = False):
			logger.trace("kqueue settime %d %s", ms, repeat)
			flags = self.flags | (not repeat and select.KQ_EV_ONESHOT or 0)
			self.kqueue.control([select.kevent(self.timer_id, filter = select.KQ_FILTER_TIMER, data = ms, flags = flags)], 0, 0)

		def cancel(self):
			logger.trace("kqueue cancel")
			with suppress(OSError):
				self.kqueue.control([select.kevent(self.timer_id, filter = select.KQ_FILTER_TIMER, flags = select.KQ_EV_DELETE)], 0, 0)

		def check_event(self, fd, ev):
			if fd == self.timer_id:
				logger.trace("kqueue timer")
				return True
			else:
				return False

		def drain(self):
			pass

	Poller = KqueuePoller
	Timer = KqueueTimer


__all__ = (
	"Poller",
	"Timer",
)
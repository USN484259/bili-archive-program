#!/usr/bin/env python3

# This file is written with the assistance of opencode-deepseek-v4-flash

import os
import re
import time
import struct
import socket
import logging
import select
from json import dumps as json_dumps, loads as json_loads
from contextlib import suppress
from collections import namedtuple
from fops import create_unix_socket

# constants
recv_bufsize = 0x10000
retry_count = 1
ns_in_ms = 1000*1000
ns_in_sec = 1000 * 1000 * 1000


# static objects

logger = logging.getLogger("bili_arch.messaging")
sctp_url_pattern = re.compile(r"^sctp://([^/]+?):(\d+)$")
topic_pattern = re.compile(r"\w+")
msg_pattern = re.compile(r"^([0-9A-Fa-f]{8}) (\S+) (\S+)$")
msg_handler = {
	"data": lambda d: d,
	"text": lambda d: d.decode(),
	"json": lambda d: json_loads(d.decode()),
}

msg_env_addr = os.environ.get("BILI_ARCH_MSG_ADDR")


# find out timerfd API for Python < 3.13 and OS that support it but not exposed by Python

timerfd_create = None
timerfd_settime_ns = None

if hasattr(os, "timerfd_create"):
	timerfd_create = os.timerfd_create
	timerfd_settime_ns = os.timerfd_settime_ns

elif not hasattr(select, "kqueue"):	# only use timerfd if kqueue is not present
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

		def timerfd_create(clockid, /, flags = 0):
			return raw_timerfd_create(clockid, flags)

		def timerfd_settime_ns(fd, /, flags = 0, initial = 0, interval = 0):
			if initial < 0:
				raise OSError(errno.EINVAL)

			old_ts = itimerspec()
			new_ts = itimerspec(timespec(interval // ns_in_sec, interval % ns_in_sec),
					timespec(initial // ns_in_sec, initial % ns_in_sec))
			raw_timerfd_settime(fd, flags, ctypes.byref(new_ts), ctypes.byref(old_ts))

			return (old_ts.it_value.tv_sec * ns_in_sec + old_ts.it_value.tv_nsec,
				old_ts.it_interval.tv_sec * ns_in_sec + old_ts.it_interval.tv_nsec)


# OS specific poll/timer implementation

class DummyPoller:
	def __init__(self, /, oneshot):
		logger.debug("creating %s %x", type(self).__name__, id(self))
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
	def is_dummy():
		return True

	@staticmethod
	def parse_event(ev):
		pass


class DummyTimer:
	def __init__(self, poller):
		logger.debug("creating %s %x", type(self).__name__, id(self))

	def close(self):
		logger.debug("closing %s %x", type(self).__name__, id(self))

	def settime(self, ms):
		pass

	def cancel(self):
		pass

	def check_event(self, fd, ev):
		return False


Poller = DummyPoller
Timer  = DummyTimer


if hasattr(select, "epoll"):
	class EpollPoller:
		def __init__(self, /, oneshot):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			self.epoll = select.epoll()
			self.epoll_mask = select.EPOLLIN | select.EPOLLRDHUP | (oneshot and select.EPOLLONESHOT or 0)
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
		def is_dummy():
			return False

		@staticmethod
		def parse_event(ev):
			if ev & select.EPOLLRDHUP:
				return "eof"
			if ev & select.EPOLLERR:
				return "err"


	Poller = EpollPoller


if timerfd_create and timerfd_settime_ns:
	class TimerfdTimer:
		def __init__(self, poller):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			self.poller = poller
			self.timerfd = timerfd_create(time.CLOCK_MONOTONIC)
			self.poller.register(self.timerfd)
			logger.trace("timerfd %d, poller %d", self.timerfd, self.poller.fileno())

		def close(self):
			logger.debug("closing %s %x", type(self).__name__, id(self))
			if self.timerfd is not None:
				with suppress(OSError):
					self.poller.unregister(self.timerfd)
					os.close(self.timerfd)
				self.timerfd = None

		def settime(self, ms):
			logger.trace("timerfd settime %d", ms)
			timerfd_settime_ns(self.timerfd, initial = ms * ns_in_ms)

		def cancel(self):
			logger.trace("timerfd cancel")
			timerfd_settime_ns(self.timerfd)

		def check_event(self, fd, ev):
			if fd != self.timerfd:
				return False
			logger.trace("timerfd timer")
			with suppress(OSError):
				os.read(self.timerfd, 8)
			return True


	Timer = TimerfdTimer


if hasattr(select, "kqueue"):
	class KqueuePoller:
		def __init__(self, /, oneshot):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			self.kqueue = select.kqueue()
			self.kqueue_flags = select.KQ_EV_ADD | (oneshot and select.KQ_EV_CLEAR or 0)
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
		def is_dummy():
			return False

		@staticmethod
		def parse_event(ev):
			if ev & select.KQ_EV_EOF:
				return "eof"
			if ev & select.KQ_EV_ERROR:
				return "err"


	class KqueueTimer:
		timer_id = 0xCDCDCDCD
		def __init__(self, poller):
			logger.debug("creating %s %x", type(self).__name__, id(self))
			self.kqueue = poller.kqueue
			self.flags = select.KQ_EV_ADD | ((poller.kqueue_flags & select.KQ_EV_CLEAR) and select.KQ_EV_ONESHOT or 0)

		def close(self):
			logger.debug("closing %s %x", type(self).__name__, id(self))
			self.cancel()

		def settime(self, ms):
			logger.trace("kqueue settime %d", ms)
			self.kqueue.control([select.kevent(self.timer_id, filter = select.KQ_FILTER_TIMER, data = ms, flags = self.flags)], 0, 0)

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


	Poller = KqueuePoller
	Timer = KqueueTimer


# server

class BaseServer:
	class Client:
		def __init__(self, socket, name):
			self.socket = socket
			self.name = name

		def close(self):
			if self.socket is not None:
				try:
					self.socket.close()
				finally:
					self.socket = None

		def fileno(self):
			return self.socket.fileno()



	def __init__(self, *args, **kwargs):
		logger.debug("creating %s %x", type(self).__name__, id(self))
		self.conn = self.on_create(*args, **kwargs)
		self.client_map = {}
		self.can_accept = True
		self.poller = Poller(oneshot = True)
		self.conn.setblocking(False)
		self.poller.register(self.conn)

		logger.trace("listening on fd %d, poll fd %d", self.conn.fileno(), self.fileno())
		self.conn.listen()
		logger.info("server started")


	def close(self):
		logger.debug("closing %s %x", type(self).__name__, id(self))
		if self.conn is not None:
			try:
				self.poller.close()
				self.conn.close()
				for client in self.client_map.values():
					client.close()
			finally:
				self.conn = None
				self.client_map.clear()

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def fileno(self):
		return self.poller.fileno()

	def is_dummy(self):
		return self.poller.is_dummy()

	def on_poll_init(self, /, oneshot):
		raise NotImplementedError()

	def on_create(self, *args):
		raise NotImplementedError()

	def on_connected(self, sock, addr):
		name = addr
		try:
			if not isinstance(sock.getsockname(), tuple):
				# try to get peer name on UNIX socket
				if hasattr(socket, "SO_PEERCRED"):
					data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 0x20)
					logger.trace(data)
					# assume pid_t is signed int
					pid = struct.unpack_from("=i", data)[0]
				elif hasattr(socket, "LOCAL_PEERCRED"):
					if not hasattr(self, "cr_pid_offset"):
						xucred = struct.Struct("@IIh16I0P")
						self.cr_pid_offset = xucred.size
					data = sock.getsockopt(0, socket.LOCAL_PEERCRED, 0x80)
					logger.trace(data)
					pid = struct.unpack_from("@i", data[self.cr_pid_offset:])[0]

				name = str(pid)
		except Exception as e:
			logger.debug("cannot find peer name %s", str(e))

		if not name:
			name = str(sock.fileno())

		logger.verbose("connected client %s", name)
		logger.debug("addr %s, fd %d", addr, sock.fileno())
		return self.Client(sock, name)


	def on_error(self, client, error, caller):
		logger.debug("error from %s", caller)
		logger.exception("error in %s for client %s", caller, client.name)
		self.drop_client(client)


	def on_disconnected(self, client):
		logger.verbose("disconnected client %s", client.name)


	def on_data_ready(self, client):
		pass

	def on_poll_event(self, fd, ev):
		logger.warning("unknown event %d %x", fd, ev)

	def drop_client(self, client):
		fd = client
		if not isinstance(client, int):
			fd = client.fileno()

		logger.debug("closing %d", fd)
		self.poller.unregister(fd)

		client = self.client_map.pop(fd, None)
		if not client:
			return
		try:
			self.on_disconnected(client)
		except Exception:
			logger.exception("exception in on_disconnected socket %s", client.name)

		try:
			client.close()
		except Exception:
			logger.exception("error in closing client %s", client.name)


	def allow_connect(self, value):
		logger.verbose("allow connect %s", value)
		self.can_accept = value
		if value:
			self.poller.rearm(self.conn.fileno())


	def send(self, client, data):
		logger.trace("send %d bytes to %s (%d)", len(data), client.name, client.fileno())
		try:
			client.socket.send(data)
		except Exception as e:
			self.on_error(client, e, "send")


	def recv(self, client, bufsize = recv_bufsize):
		logger.trace("recv from %s (%d)", client.name, client.fileno())
		try:
			data = client.socket.recv(bufsize)
			logger.trace("recv %d bytes", len(data))
			return data or None

		except BlockingIOError:
			logger.trace("recv EAGAIN")
			self.poller.rearm(client.fileno())
			return None
		except Exception as e:
			self.on_error(client, e, "recv")
			return None


	def foreach_client(self, func, *args, **kwargs):
		client_snapshot = list(self.client_map.values())
		for client in client_snapshot:
			func(client, *args, **kwargs)


	def wait(self, timeout = None):
		logger.trace("wait %s", timeout)
		for fd, ev in self.poller.poll(timeout):
			logger.trace("fd %d, ev %x", fd, ev)

			if fd == self.conn.fileno():
				logger.debug("incoming connections")
				while True:
					if not self.can_accept:
						logger.verbose("not accepting more connections")
						break
					try:
						sock, addr = self.conn.accept()
						logger.verbose("accepted new connection %s, fd %d", addr, sock.fileno())
					except BlockingIOError:
						logger.verbose("done with connections")
						self.poller.rearm(self.conn.fileno())
						break
					sock.setblocking(False)
					try:
						self.client_map[sock.fileno()] = self.on_connected(sock, addr)
					except Exception:
						logger.exception("exception in on_connected for client %s", addr)
						self.client_map.pop(sock.fileno(), None)
						sock.close()
					else:
						self.poller.register(sock.fileno())

			else:
				client = self.client_map.get(fd)
				if client is not None:
					try:
						logger.debug("data_ready for client %s (%d)", client.name, fd)
						self.on_data_ready(client)
					except Exception:
						logger.exception("exception in on_data_ready for client %s", client.name)
				else:
					logger.debug("poll_event for fd %d", fd)
					self.on_poll_event(fd, ev)

			# handle closed socket
			if self.poller.parse_event(ev) is not None:
				if fd == self.conn.fileno():
					raise RuntimeError("server socket error %x", ev)
				else:
					self.drop_client(fd)


def parse_sctp_url(url):
	sctp_match = sctp_url_pattern.fullmatch(url)
	if sctp_match:
		return sctp_match[1], int(sctp_match[2])
	else:
		return None, None


if hasattr(socket, "SOCK_SEQPACKET"):
	def serve_unix(path, mode):
		logger.verbose("serving unix socket %s", path)
		sock = create_unix_socket(path, sock_type = socket.SOCK_SEQPACKET, mode = mode)
		logger.debug("sockfd %d", sock.fileno())
		return sock


	def connect_unix(path):
		logger.verbose("connecting unix socket %s", path)
		sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		try:
			sock.connect(path)
			logger.debug("sockfd %d", sock.fileno())
			return sock
		except:
			sock.close()
			raise


def serve_sctp(addr, port):
	logger.verbose("serving sctp %s port %d", addr, port)
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP)
	try:
		sock.bind((addr, port))
		sock.listen()
		logger.debug("sockfd %d", sock.fileno())
		return sock
	except:
		sock.close()
		raise


def connect_sctp(addr, port):
	logger.verbose("connecting sctp %s port %d", addr, port)
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP)
	try:
		sock.connect((addr, port))
		logger.debug("sockfd %d", sock.fileno())
		return sock
	except:
		sock.close()
		raise


class MessagingServer(BaseServer):
	# override
	def on_create(self, path, *args):
		logger.verbose("opening %s", path)
		(addr, port) = parse_sctp_url(path)
		if addr and port:
			return serve_sctp(addr, port)
		else:
			return serve_unix(path, args and args[0] or 0o600)


	# override
	def on_connected(self, *args):
		client = super().on_connected(*args)
		client.subscriptions = set()
		return client


	# override
	def on_data_ready(self, client):
		while True:
			data = self.recv(client)
			logger.trace(data)
			if data is None:
				break
			self.handle_message(client, data)


	def handle_message(self, client, data):
		try:
			sp = data.split(b'\n', maxsplit = 1)
			header = sp[0].decode()
			msg_match = msg_pattern.match(header)
			if not msg_match:
				raise ValueError(header)
			size = msg_match[1]
			_type = msg_match[2]
			topic = msg_match[3]
			logger.verbose("from %s, topic %s, type %s, size %s", client.name, topic, _type, size)
			if _type == 'ctrl':
				self.handle_client_cmd(client, topic, sp[1])
			else:
				self.foreach_client(self.forward_message, topic, data)
			return topic
		except Exception as e:
			logger.warning("bad message: %s", str(e))


	def handle_client_cmd(self, client, cmd, data):
		if cmd == "subscribe":
			subscriptions = set()
			for topic in data.decode().split():
				logger.debug("subscribe %s", topic)
				if topic_pattern.fullmatch(topic):
					subscriptions.add(topic)
				elif topic == '*':
					subscriptions.clear()
					subscriptions.add('*')
					break
			client.subscriptions = subscriptions
			logger.verbose("client %s, subscription %s", client.name, ' '.join(subscriptions))
		else:
			logger.warning("unknown ctrl command %s", cmd)


	def forward_message(self, client, topic, data):
		logger.debug("topic %s, client %s (%d), subscription %s", topic, client.name, client.fileno(), ' '.join(client.subscriptions))
		if '*' in client.subscriptions or topic in client.subscriptions:
			self.send(client, data)


# client

class BaseClient:
	def __init__(self, *args, **kwargs):
		logger.debug("creating %s %x", type(self).__name__, id(self))
		self.conn = self.on_create(*args, **kwargs)
		self.poll = None
		self.conn.setblocking(False)

	def close(self):
		logger.debug("closing %s %x", type(self).__name__, id(self))
		if self.conn is not None:
			try:
				self.conn.close()
			finally:
				self.conn = None

	def fileno(self):
		return self.conn.fileno()

	def on_create(self, *args, **kwargs):
		raise NotImplementedError()

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def send(self, data):
		try:
			logger.trace("send %d bytes", len(data))
			self.conn.send(data)
			return True
		except BlockingIOError:
			logger.trace("send EAGAIN")
			return False

	def recv(self, bufsize = recv_bufsize):
		try:
			logger.trace("recv")
			data = self.conn.recv(bufsize)
			logger.trace("recv %d bytes", len(data))
			return data
		except BlockingIOError:
			logger.trace("recv EAGAIN")
			return None

	def wait(self, timeout = None):
		logger.trace("wait %s", timeout)
		if self.poll is None:
			self.poll = select.poll()
			self.poll.register(self.conn, select.POLLIN | select.POLLRDHUP)

		self.poll.poll(timeout and int(timeout * 1000))


class MessagingClient:
	class Client(BaseClient):
		# override
		def on_create(self, path, *args):
			logger.verbose("connecting %s", path)
			(addr, port) = parse_sctp_url(path)
			if addr and port:
				return connect_sctp(addr, port)
			else:
				return connect_unix(path)


	def __init__(self, addr = None, /, allow_dummy = True, *, reconnect = None):
		logger.debug("creating %s %x", type(self).__name__, id(self))
		if reconnect and Timer is DummyTimer:
			logger.warning("no timer support on this platform, reconnect do not work")

		self.msg_addr = addr or msg_env_addr
		self.allow_dummy = allow_dummy
		self.reconnect_ms = reconnect and int(reconnect * 1000)
		self.poller = Poller(oneshot = False)
		self.timer = reconnect and Timer(self.poller)
		self.conn = None
		self.last_connected = False
		self.shutdown = False
		self.subscription = ""

		logger.verbose("msg_addr %s, allow_dummy %s, reconnect %s", self.msg_addr, self.allow_dummy, self.reconnect_ms)
		if allow_dummy or self.msg_addr:
			self.check_connection()
		else:
			raise ValueError("missing addr")


	def check_connection(self):
		if self.conn is not None:
			return True

		if self.msg_addr:
			try:
				logger.verbose("trying to connect %s", self.msg_addr)
				self.conn = self.Client(self.msg_addr)
				self.poller.register(self.conn)
				self.shutdown = False
				if self.subscription:
					self._do_subscribe()
				self.on_connected()
				if self.timer:
					self.timer.cancel()
				self.last_connected = True
				return True
			except Exception as e:
				logger.verbose("cannot open %s: %s", self.msg_addr, str(e))
				if self.timer:
					logger.verbose("reconnect timer %d", self.reconnect_ms)
					self.timer.settime(self.reconnect_ms)

				if not self.allow_dummy:
					raise

				if self.last_connected:
					logger.notice("cannot open %s, use dummy", self.msg_addr)
				self.last_connected = False

		return False


	def reconnect(self):
		if self.conn is not None:
			self.poller.unregister(self.conn)
			try:
				self.conn.close()
			finally:
				self.conn = None

			self.on_disconnected()

		return self.check_connection()


	def close(self):
		logger.debug("closing %s %x", type(self).__name__, id(self))
		if self.timer is not None:
			self.timer.close()
		self.poller.close()

		if self.conn is not None:
			with suppress(OSError):
				self.conn.close()
			self.conn = None

	def fileno(self):
		return self.poller.fileno()

	def is_dummy(self):
		return self.poller.is_dummy()

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def _send_out(self, payload, caller):
		retry = 0
		while True:
			logger.debug("send retry %d", retry)
			try:
				rc = self.conn.send(payload)
			except OSError as e:
				self.on_error(e, retry, caller)
				if retry >= retry_count or not self.reconnect():
					return False
				else:
					retry += 1
					continue
			else:
				if not rc:
					self.on_congestion()
				return rc

		return False

	def _do_subscribe(self):
		logger.verbose("subscribing %s", self.subscription)
		payload = bytearray("00000000 ctrl subscribe\n" + self.subscription, 'utf-8')
		payload[0:8] = b"%08x" % len(payload)
		return self._send_out(payload, "subscribe")


	def subscribe(self, *subscriptions):
		self.subscription = " ".join(subscriptions) or '*'
		if not self.check_connection():
			return False
		return self._do_subscribe()


	def send(self, topic, /, data = None, json = None, *, raw = False):
		if (data is None) == (json is None):
			raise RuntimeError("invalid parameter")

		if not self.check_connection():
			return False

		if raw:
			payload = data
		else:
			content_type = "text"
			if json is not None:
				content_type = "json"
				data = json_dumps(json, ensure_ascii = False)
			if isinstance(data, str):
				data = data.encode()
			else:
				content_type = "data"

			logger.debug("send %s %s %d", topic, content_type, len(data))

			payload = bytearray("00000000 %s %s\n" % (content_type, topic), 'utf-8')
			payload += data
			payload[0:8] = b"%08x" % len(payload)

		logger.trace(payload)
		return self._send_out(payload, "send")


	def recv(self, *subscriptions, raw = False):
		if not self.check_connection():
			return None, None

		retry = 0
		while True:
			logger.debug("recv retry %d", retry)
			try:
				data = self.conn.recv()
				logger.trace(data)
				retry = 0
			except OSError as e:
				self.on_error(e, retry, "recv")
				if retry >= retry_count or not self.reconnect():
					return None, None
				else:
					retry += 1
					continue

			if not data:
				if self.shutdown:
					self.reconnect()

				return None, None

			try:
				sp = data.split(b'\n', maxsplit = 1)
				header = sp[0].decode()
				content = sp[1]
				msg_match = msg_pattern.match(header)

				size = int(msg_match[1], 0x10)
				if size != len(data):
					raise ValueError("invalid message")
				content_type = msg_match[2]
				topic = msg_match[3]
			except Exception as e:
				logger.warning("bad message: %s", str(e))

			else:
				logger.debug("recv %s %s %d", topic, content_type, len(content))
				if (not subscriptions) or topic in subscriptions:
					if raw:
						return topic, data
					try:
						func = msg_handler.get(content_type)
						if not callable(func):
							raise ValueError("unknown type %s", content_type)
						return topic, func(content)
					except Exception as e:
						logger.warning("bad message: %s", str(e))

	def wait(self, timeout = None):
		logger.debug("waiting %s", timeout)
		self.check_connection()
		rc = False
		for fd, ev in self.poller.poll(timeout):
			logger.trace("fd %d, ev %x", fd, ev)

			if self.timer and self.timer.check_event(fd, ev):
				logger.verbose("reconnect timer")
				with suppress(Exception):
					self.check_connection()
				continue

			if self.conn is None or fd != self.conn.fileno():
				logger.warning("unknown fd %d, ev %x", fd, ev)
				continue

			try:
				rc = True
				logger.debug("data_ready")
				self.on_data_ready()
			except Exception:
				logger.exception("exception in on_data_ready")

			ev_str = self.poller.parse_event(ev)
			if ev_str == "eof":
				self.shutdown = True

			if ev_str == "err":
				logger.verbose("connection error, reconnecting")
				self.reconnect()
				return False

		return rc

	def on_data_ready(self):
		pass

	def on_error(self, e, retry, caller):
		logger.error("retry %d in %s: %s", retry, caller, str(e))

	def on_congestion(self):
		logger.warning("send congestion")

	def on_disconnected(self):
		logger.verbose("disconnected")

	def on_connected(self):
		logger.verbose("connected")


# message relay

class MessagingRelay(MessagingServer):
	def __init__(self, sock_path, /, mode, upstream, allow_dummy = True, *args):
		super().__init__(sock_path, mode)
		self.up_client = MessagingClient(upstream, allow_dummy, *args)
		self.forward_subscription = set()
		self.poller.register(self.up_client.fileno())
		logger.info("relay running on %s, upstream %s", sock_path, upstream)


	# None for no-op, "*" for all, empty list to clear
	def subscribe(self, *, listen = None, forward = None):
		if listen is not None:
			logger.verbose("listen subscription %s", " ".join(listen))
			self.up_client.subscribe(*listen)

		if forward is not None:
			logger.verbose("forward subscription %s", " ".join(forward))
			self.forward_subscription = set(forward)


	# override
	def on_poll_event(self, fd, ev):
		if fd == self.up_client.fileno():
			logger.debug("client event")
			self.up_client.wait(0)
			while True:
				topic, data = self.up_client.recv(raw = True)
				if not topic:
					break
				self.foreach_client(self.forward_message, topic, data)

			if self.poller.parse_event(ev):
				raise RuntimeError("client socket error %x", ev)
			self.poller.rearm(self.up_client.fileno())
		else:
			return super().on_poll_event(fd, ev)


	# override
	def on_data_ready(self, client):
		while True:
			data = self.recv(client)
			if data is None:
				break
			topic = self.handle_message(client, data)
			if not topic:
				continue
			if '*' in self.forward_subscription or topic in self.forward_subscription:
				self.up_client.send(topic, data, raw = True)


# server entrypoint

if __name__ == "__main__":
	import argparse
	import sys
	from utils import logger_init

	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-m", "--file-mode")
	parser.add_argument("--relay")
	parser.add_argument("-l", "--listen", nargs = '*')
	parser.add_argument("-f", "--forward", nargs = '*')
	parser.add_argument("socket")

	args = parser.parse_args()
	logger_init(args.verbose)
	logger.trace(args)

	mode = 0o600
	if args.file_mode:
		mode = int(args.file_mode, base = 8)
		logger.debug("file mode %o", mode)

	if args.relay:
		logger.info("running in relay mode")
		server = MessagingRelay(args.socket, mode, args.relay)
		server.subscribe(listen = args.listen or '*', forward = args.forward or '*')
	else:
		logger.info("running in server mode")
		server = MessagingServer(args.socket, mode)

	with server:
		while True:
			server.wait()


# exports

__all__ = (
	"BaseServer",
	"UnixSocketServer",
	"MessagingServer",
	"BaseClient",
	"UnixSocketClient",
	"MessagingClient",
	"MessagingRelay",
)

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
ms2ns = 1000*1000
activate = hasattr(select, "epoll") and hasattr(socket, "SOCK_SEQPACKET")
if activate:
	_epoll_mask = select.EPOLLIN | select.EPOLLRDHUP | select.EPOLLONESHOT
else:
	_epoll_mask = 0

# static objects

logger = logging.getLogger("bili_arch.messaging")
topic_pattern = re.compile(r"\w+")
msg_pattern = re.compile(r"^([0-9A-Fa-f]{8}) (\S+) (\S+)$")
msg_handler = {
	"data": lambda d: d,
	"text": lambda d: d.decode(),
	"json": lambda d: json_loads(d.decode()),
}

msg_env_addr = os.environ.get("BILI_ARCH_MSG_ADDR")


# server

class BaseServer:
	class Client:
		def __init__(self, socket, name):
			self.socket = socket
			self.name = name

	epoll_mask = _epoll_mask

	def __init__(self, *args, **kwargs):
		self.conn = self.on_create(*args, **kwargs)
		self.epoll = select.epoll()
		self.client_map = {}
		self.can_accept = True
		self.conn.setblocking(False)
		self.epoll.register(self.conn, self.epoll_mask)
		logger.debug("listening on fd %d, epoll fd %d", self.conn.fileno(), self.epoll.fileno())
		self.conn.listen()
		logger.info("server started")


	def close(self):
		if self.conn is not None:
			try:
				self.epoll.close()
				self.conn.close()
				for client in self.client_map.values():
					client.socket.close()
			finally:
				self.conn = None
				self.epoll = None
				self.client_map.clear()

	def fileno(self):
		return self.epoll.fileno()

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def on_create(self, *args):
		raise NotImplementedError()

	def on_connected(self, sock, addr):
		name = addr
		# try to get peer name
		try:
			data = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 0x20)
			# assume pid_t is signed int
			pid = struct.unpack_from("=i", data)[0]
			name = str(pid)
		except Exception as e:
			logger.debug("cannot find peer name %s", str(e))

		if not name:
			name = str(sock.fileno())

		logger.info("connected client %s", name)
		return self.Client(sock, name)


	def on_error(self, client, error, caller):
		logger.exception("error in %s for client %s", caller, client.name)
		self.drop_client(client)


	def on_disconnected(self, client):
		logger.info("disconnected client %s", client.name)


	def on_data_ready(self, client):
		pass

	def on_epoll_event(self, fd, ev):
		logger.warning("unknown event %d %x", fd, ev)

	def drop_client(self, client):
		fd = client
		if not isinstance(client, int):
			fd = client.socket.fileno()

		logger.debug("closing %d", fd)

		with suppress(KeyError, OSError):
			self.epoll.unregister(fd)

		client = self.client_map.pop(fd, None)
		if not client:
			return
		try:
			self.on_disconnected(client)
		except Exception:
			logger.exception("exception in on_disconnected socket %s", client.name)

		try:
			client.socket.close()
		except Exception:
			logger.exception("error in closing client %s", client.name)


	def allow_connect(self, value):
		logger.debug("allow connect %s", bool(value))
		self.can_accept = value
		if value:
			self.epoll.modify(self.conn, self.epoll_mask)


	def send(self, client, data):
		try:
			client.socket.send(data)
		except Exception as e:
			self.on_error(client, e, "send")


	def recv(self, client, bufsize = recv_bufsize):
		try:
			data = client.socket.recv(bufsize)
			return data or None

		except BlockingIOError:
			self.epoll.modify(client.socket, self.epoll_mask)
			return None
		except Exception as e:
			self.on_error(client, e, "recv")
			return None


	def foreach_client(self, func, *args, **kwargs):
		client_snapshot = list(self.client_map.values())
		for client in client_snapshot:
			func(client, *args, **kwargs)


	def wait(self, timeout = None):
		for fd, ev in self.epoll.poll(timeout):
			logger.debug("fd %d, ev %x", fd, ev)

			if fd == self.conn.fileno():
				logger.debug("incoming connections")
				while True:
					if not self.can_accept:
						logger.debug("not accepting more connections")
						break
					try:
						sock, addr = self.conn.accept()
						logger.debug("accepted new connection %s, fd %d", str(addr), sock.fileno())
					except BlockingIOError:
						logger.debug("done with connections")
						self.epoll.modify(self.conn, self.epoll_mask)
						break
					sock.setblocking(False)
					try:
						self.client_map[sock.fileno()] = self.on_connected(sock, addr)
					except Exception:
						logger.exception("exception in on_connected for client %s", addr)
						self.client_map.pop(sock.fileno(), None)
						sock.close()
					else:
						self.epoll.register(sock, self.epoll_mask)

			else:
				client = self.client_map.get(fd)
				if client is not None:
					try:
						self.on_data_ready(client)
					except Exception:
						logger.exception("exception in on_data_ready for client %s", client.name)
				else:
					self.on_epoll_event(fd, ev)

			# handle closed socket
			if ev & (select.EPOLLRDHUP | select.EPOLLERR):
				if fd == self.conn.fileno():
					raise RuntimeError("server socket error %x", ev)
				else:
					self.drop_client(fd)


def serve_unix(path, mode):
	logger.info("opening unix socket %s", path)
	return create_unix_socket(path, sock_type = socket.SOCK_SEQPACKET, mode = mode)

def serve_sctp(addr, port):
	logger.info("connecting sctp %s port %d", path, port)
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP)
	try:
		sock.bind((addr, port))
		sock.listen()
		return sock
	except:
		sock.close()
		raise

def connect_unix(path):
	sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
	try:
		sock.connect(path)
		return sock
	except:
		sock.close()
		raise

def connect_sctp(addr, port):
	sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP)
	try:
		sock.connect((addr, port))
		return sock
	except:
		sock.close()
		raise


class MessagingServer(BaseServer):
	# override
	def on_create(self, sock_path, *args):
		return serve_unix(sock_path, args and args[0] or 0o600)

	# override
	def on_connected(self, *args):
		client = super().on_connected(*args)
		client.subscriptions = set()
		return client


	# override
	def on_data_ready(self, client):
		while True:
			data = self.recv(client)
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
			logger.debug("from %s, topic %s, type %s, size %s", client.name, topic, _type, size)
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
				if topic_pattern.fullmatch(topic):
					subscriptions.add(topic)
				elif topic == '*':
					subscriptions.clear()
					subscriptions.add('*')
					break
			client.subscriptions = subscriptions
		else:
			logger.warning("unknown ctrl command %s", cmd)


	def forward_message(self, client, topic, data):
		if '*' in client.subscriptions or topic in client.subscriptions:
			self.send(client, data)


# client

class BaseClient:
	def __init__(self, *args, **kwargs):
		self.conn = self.on_create(*args, **kwargs)
		self.poll = None
		self.conn.setblocking(False)

	def close(self):
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
			self.conn.send(data)
			return True
		except BlockingIOError:
			return False

	def recv(self, bufsize = recv_bufsize):
		try:
			return self.conn.recv(bufsize)
		except BlockingIOError:
			return None

	def wait(self, timeout = None):
		if self.poll is None:
			self.poll = select.poll()
			self.poll.register(self.conn, select.POLLIN | select.POLLRDHUP)

		self.poll.poll(timeout and int(timeout * 1000))


class MessagingClient:
	epoll_mask = _epoll_mask

	class Client(BaseClient):
		# override
		def on_create(self, path, *args):
			return connect_unix(path)

	def __init__(self, addr = None, /, allow_dummy = True, *, reconnect = None):
		self.msg_addr = addr or msg_env_addr
		self.allow_dummy = allow_dummy
		self.reconnect_interval = reconnect and int(reconnect)
		self.epoll = select.epoll()
		self.timer = os.timerfd_create(time.CLOCK_MONOTONIC)
		self.conn = None
		self.shutdown = False
		self.subscription = ""

		self.epoll.register(self.timer, self.epoll_mask)

		logger.debug("msg_addr %s, allow_dummy %s, reconnect %s", self.msg_addr, str(self.allow_dummy), str(self.reconnect_interval))
		if allow_dummy or self.msg_addr:
			self.check_connection()
		else:
			raise ValueError("missing addr")

	def check_connection(self):
		if self.conn is not None:
			return True

		if self.msg_addr:
			try:
				self.conn = self.Client(self.msg_addr)
				self.epoll.register(self.conn, self.epoll_mask)
				self.shutdown = False
				if self.subscription:
					self._do_subscribe()
				self.on_connected()
				if self.reconnect_interval:
					os.timerfd_settime_ns(self.timer)
				return True
			except Exception as e:
				logger.debug("cannot open %s: %s", self.msg_addr, str(e))
				if self.reconnect_interval:
					os.timerfd_settime_ns(self.timer, initial = self.reconnect_interval * ms2ns)
					self.epoll.modify(self.timer, self.epoll_mask)

				if not self.allow_dummy:
					raise
				else:
					logger.warning("cannot open %s, use dummy", self.msg_addr)

		return False


	def reconnect(self):
		if self.conn is not None:
			with suppress(KeyError, OSError):
				self.epoll.unregister(self.conn)
			try:
				self.conn.close()
			finally:
				self.conn = None

			self.on_disconnected()

		return self.check_connection()


	def close(self):
		if self.timer is not None:
			with suppress(OSError):
				os.close(self.timer)
			self.timer = None
		if self.conn is not None:
			with suppress(OSError):
				self.conn.close()
			self.conn = None
		if self.epoll is not None:
			with suppress(OSError):
				self.epoll.close()
			self.epoll = None

	def fileno(self):
		return self.epoll.fileno()

	def is_dummy(self):
		return self.conn is None

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

			payload = bytearray("00000000 %s %s\n" % (content_type, topic), 'utf-8')
			payload += data
			payload[0:8] = b"%08x" % len(payload)

		return self._send_out(payload, "send")


	def recv(self, *subscriptions, raw = False):
		if not self.check_connection():
			return None, None

		retry = 0
		while True:
			logger.debug("recv retry %d", retry)
			try:
				data = self.conn.recv()
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
				else:
					self.epoll.modify(self.conn, self.epoll_mask)
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
				logger.debug("recv %s", topic)
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
		self.check_connection()
		rc = False
		for fd, ev in self.epoll.poll(timeout):
			logger.debug("fd %d, ev %x", fd, ev)

			if fd == self.timer:
				logger.debug("reconnecting")
				with suppress(OSError):
					os.read(self.timer, 8)
				with suppress(Exception):
					self.check_connection()
				continue

			if self.conn is None or fd != self.conn.fileno():
				logger.warning("unknown fd %d, ev %x", fd, ev)
				continue

			if ev & select.EPOLLIN:
				try:
					rc = True
					self.on_data_ready()
				except Exception:
					logger.exception("exception in on_data_ready")

			if ev & select.EPOLLRDHUP:
				self.shutdown = True

			if ev & select.EPOLLERR:
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
		logger.debug("disconnected")

	def on_connected(self):
		logger.debug("connected")


# dummy client for platforms without epoll and SOCK_SEQPACKET

class DummyMessagingClient:
	def __init__(self, addr = None, /, allow_dummy = True, *args):
		if not allow_dummy:
			raise NotImplementedError("messaging client requires Linux (epoll and SOCK_SEQPACKET), set allow_dummy = True to run without messaging")

		self.poll = select.poll()
		self._pipe_r = -1
		self._pipe_w = -1

		# an empty pipe whose write end stays open is never readable:
		# nothing is ever written, and the open write end prevents EOF
		self._pipe_r, self._pipe_w = os.pipe()
		self.poll.register(self._pipe_r, select.POLLIN)


	def close(self):
		for fd in (self._pipe_r, self._pipe_w):
			if fd >= 0:
				with suppress(OSError):
					os.close(fd)
		self._pipe_r = -1
		self._pipe_w = -1


	def fileno(self):
		return self._pipe_r

	def is_dummy(self):
		return True

	def check_connection(self):
		return False

	def reconnect(self):
		return False

	def subscribe(self, *subscriptions):
		return False

	def send(self, topic, /, data = None, json = None):
		return False

	def recv(self, *subscriptions):
		return None, None

	def wait(self, timeout = None):
		self.poll.poll(timeout and int(timeout * 1000))
		return False

	def on_data_ready(self):
		pass

	def on_error(self, e, retry, caller):
		pass

	def on_congestion(self):
		pass

	def on_disconnected(self):
		pass

	def on_connected(self):
		pass

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()


if not activate:
	MessagingClient = DummyMessagingClient


# message relay

class MessagingRelay(MessagingServer):
	def __init__(self, sock_path, /, mode, upstream, allow_dummy = True, *args):
		super().__init__(sock_path, mode)
		self.up_client = MessagingClient(upstream, allow_dummy, *args)
		self.forward_subscription = set()
		self.epoll.register(self.up_client, self.epoll_mask)
		logger.info("relay running on %s, upstream %s", sock_path, upstream)


	# None for no-op, "*" for all, empty list to clear
	def subscribe(self, *, listen = None, forward = None):
		if listen is not None:
			self.up_client.subscribe(*listen)

		if forward is not None:
			self.forward_subscription = set(forward)


	# override
	def on_epoll_event(self, fd, ev):
		if fd == self.up_client.fileno():
			self.up_client.wait(0)
			while True:
				topic, data = self.up_client.recv(raw = True)
				if not topic:
					break
				self.foreach_client(self.forward_message, topic, data)

			if ev & (select.EPOLLRDHUP | select.EPOLLERR):
				raise RuntimeError("client socket error %x", ev)
			self.epoll.modify(self.up_client, self.epoll_mask)
		else:
			return super().on_epoll_event(fd, ev)


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

	if not activate:
		sys.exit("messaging server requires Linux (epoll and SOCK_SEQPACKET)")

	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-m", "--file-mode")
	parser.add_argument("--relay")
	parser.add_argument("-l", "--listen", nargs = '*')
	parser.add_argument("-f", "--forward", nargs = '*')
	parser.add_argument("socket")

	args = parser.parse_args()
	logger_init(args.verbose)

	mode = 0o600
	if args.file_mode:
		mode = int(args.file_mode, base = 8)

	if args.relay:
		server = MessagingRelay(args.socket, mode, args.relay)
		server.subscribe(listen = args.listen or '*', forward = args.forward or '*')
	else:
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

#!/usr/bin/env python3

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


# static objects

logger = logging.getLogger("bili_arch.messaging")
msg_pattern = re.compile(r"^([0-9A-Fa-f]{8}) (\S+) (\S+)$")
msg_handler = {
	"data": lambda d: d,
	"text": lambda d: d.decode(),
	"json": lambda d: json_loads(d.decode()),
}

msg_env_addr = os.environ.get("BILI_ARCH_MSG_ADDR")


# server

class BaseServer:
	Client = namedtuple("Client", ("socket", "name"))
	epoll_mask = select.EPOLLIN | select.EPOLLRDHUP | select.EPOLLONESHOT


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


	def broadcast(self, data, *, exclude = None):
		client_snapshot = list(self.client_map.values())
		for client in client_snapshot:
			if exclude and client in exclude:
				continue
			try:
				client.socket.send(data)
			except Exception as e:
				self.on_error(client, e, "broadcast")


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

			# handle closed socket
			if ev & select.EPOLLRDHUP:
				if fd == self.conn.fileno():
					raise RuntimeError("server socket disconnected")
				else:
					self.drop_client(fd)


class UnixSocketServer(BaseServer):
	def on_create(self, sock_path, /, mode):
		logger.info("opening unix socket %s", sock_path)
		return create_unix_socket(sock_path, sock_type = socket.SOCK_SEQPACKET, mode = mode)


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


class UnixSocketClient(BaseClient):
	def on_create(self, sock_path):
		sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
		try:
			sock.connect(sock_path)
			return sock
		except Exception:
			sock.close()
			raise


# public functions as client library

class MessagingClient:
	epoll_mask = select.EPOLLIN | select.EPOLLRDHUP | select.EPOLLONESHOT

	def __init__(self, addr = None, /, retry = 1, allow_dummy = True):
		self.msg_addr = addr or msg_env_addr
		self.allow_dummy = allow_dummy
		self.retry = retry
		self.epoll = select.epoll()
		self.conn = None
		self.shutdown = False

		if allow_dummy or self.msg_addr:
			self.check_connection()
		else:
			raise ValueError("missing addr")

	def check_connection(self):
		if self.conn is not None:
			return True

		if self.msg_addr:
			try:
				self.conn = UnixSocketClient(self.msg_addr)
				self.epoll.register(self.conn, self.epoll_mask)
				self.shutdown = False
				self.on_connected()
				return True
			except Exception as e:
				logger.debug("cannot open %s: %s", self.msg_addr, str(e))
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
		fds = []
		if self.conn is not None:
			fds.append(self.conn)
			self.conn = None

		if self.epoll is not None:
			fds.append(self.epoll)
			self.epoll = None

		if fds:
			try:
				fds.pop().close()
			finally:
				if fds:
					fds.pop().close()

	def fileno(self):
		return self.epoll.fileno()

	def is_dummy(self):
		return self.conn is None

	def __enter__(self):
		return self

	def __exit__(self, *args):
		self.close()

	def send(self, topic, /, data = None, json = None):
		if (data is None) == (json is None):
			raise RuntimeError("invalid parameter")

		if not self.check_connection():
			return False

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

		retry = 0
		while True:
			logger.debug("send %s, retry %d", topic, retry)
			try:
				rc = self.conn.send(payload)
			except OSError as e:
				self.on_error(e, retry, "send")
				if retry >= self.retry:
					raise
				elif not self.reconnect():
					return False
				else:
					retry += 1
					continue
			else:
				if not rc:
					self.on_congestion()
				return rc

		return False

	def recv(self, *subscriptions):
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
				if retry >= self.retry:
					raise
				elif not self.reconnect():
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


# server entrypoint

if __name__ == "__main__":
	import argparse
	from utils import logger_init

	class MessagingServer(UnixSocketServer):
		# override
		def on_data_ready(self, client):
			while True:
				data = self.recv(client)
				if data is None:
					break
				if logger.isEnabledFor(logging.DEBUG):
					with suppress(Exception):
						header = data.split(b'\n', maxsplit = 1)[0].decode()
						msg_match = msg_pattern.match(header)
						if msg_match:
							logger.debug("from %s, topic %s, type %s, size %s", client.name, msg_match[3], msg_match[2], msg_match[1])

				self.broadcast(data, exclude = (client, ))


	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-m", "--file-mode")
	parser.add_argument("socket")

	args = parser.parse_args()
	logger_init(args.verbose)

	mode = 0o666
	if args.file_mode:
		mode = int(args.file_mode, base = 8)

	with MessagingServer(args.socket, mode = mode) as server:
		while True:
			server.wait()


# exports

__all__ = (
	"BaseServer",
	"UnixSocketServer",
	"BaseClient",
	"UnixSocketClient",
	"MessagingClient",
)

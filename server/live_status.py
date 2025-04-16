#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import time
import socket
import argparse
from contextlib import suppress
from fcgi_server import FcgiServer, HttpResponseMixin
from fastcgi import FcgiHandler

class live_status_handler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			timestamp = time.monotonic()
			if self.server.timestamp and timestamp - self.server.timestamp < self.server.interval:
				return self.send_response(200, "application/json", self.server.cached_result)

			self.server.timestamp = timestamp

			conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
			data = bytes()
			try:
				conn.connect(self.server.socket_path)
				while True:
					chunk = conn.recv(0x1000)
					if not chunk:
						break
					data += chunk
			finally:
				conn.close()

			self.server.cached_result = data
			return self.send_response(200, "application/json", data)
		except Exception as e:
			print(str(e), file = sys.stderr)
			return self.send_response(500)


class LiveStatusServer(FcgiServer):
	def __init__(self, handler, socket_path, interval):
		if interval <= 0:
			raise ValueError("invalid interval " + str(interval))
		FcgiServer.__init__(self, handler)
		self.socket_path = socket_path
		self.interval = interval
		self.timestamp = None
		self.cached_result = {}


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--status-interval", type = int, default = 2)
	parser.add_argument("--status-socket")
	parser.add_argument("--danmaku-root")
	parser.add_argument("--danmaku-socket")

	args = parser.parse_args()

	# danmaku_server is a standalone service running on the host
	# it works with mod_wstunnel and is not related to fastCGI
	# to make danmaku_server start with lighty, attach it to
	# some FCGI service, such as live_status here
	if args.danmaku_root and args.danmaku_socket:
		pid = os.fork()
		if pid == 0:
			try:
				from danmaku_server import DanmakuServer, danmaku_handler

				with suppress(OSError):
					os.unlink(args.danmaku_socket)
				with DanmakuServer(args.danmaku_root, args.danmaku_socket, danmaku_handler) as server:
					server.serve_forever(poll_interval = 600)

			finally:
				os._exit(0)
		else:
			os.waitpid(pid, os.WNOHANG)

	with LiveStatusServer(live_status_handler, args.status_socket, args.status_interval) as server:
		server.serve_forever(poll_interval = 600)

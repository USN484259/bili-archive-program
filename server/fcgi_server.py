#!/usr/bin/env python3

import sys
import json
import socket
from socketserver import BaseServer, ThreadingMixIn


class FcgiServer(BaseServer):

	def __init__(self, handler, sockfd = sys.stdin.fileno()):
		BaseServer.__init__(self, None, handler)
		self.socket = socket.socket(fileno = sockfd)

	def server_close(self):
		self.socket.close()

	def fileno(self):
		return self.socket.fileno()

	def get_request(self):
		return self.socket.accept()

	def shutdown_request(self, request):
		try:
			request.shutdown(socket.SHUT_WR)
		except OSError:
			pass
		self.close_request(request)

	def close_request(self, request):
		request.close()


class FcgiThreadingServer(ThreadingMixIn, FcgiServer):
	pass


class HttpResponseMixin:
	status_table = {
		200:	"200 OK",
		206:	"206 Partial Content",
		400:	"400 Bad Request",
		403:	"403 Forbidden",
		404:	"404 Not Found",
		405:	"405 Method Not Allowed",
		416:	"416 Range Not Satisfiable",
		429:	"429 Too Many Requests",
		500:	"500 Internal Server Error",
		502:	"502 Bad Gateway",
		504:	"504 Gateway Timeout",
	}

	def send_response(self, code, /, mime_type = "text/plain", data = None, *, extra_headers = []):
		resp_name = self.status_table.get(code)
		if not resp_name:
			return self.send_response(502)

		mime_ext = ""
		if mime_type.startswith("text/"):
			mime_ext = "; charset=utf-8"
		resp_str = "Content-type: %s%s\r\nStatus: %s\r\n" % (mime_type, mime_ext, resp_name)
		self["stdout"].write(resp_str.encode())
		for header in extra_headers:
			self["stdout"].write(header.encode() + b"\r\n")
		self["stdout"].write(b"\r\n")
		if data:
			if callable(data):
				for chunk in data():
					self["stdout"].write(chunk)
				return

			if isinstance(data, str):
				data = data.encode()
			elif (not isinstance(data, bytes)) and ("json" in mime_type):
				data = json.dumps(data, indent = '\t', ensure_ascii = False).encode()

			self["stdout"].write(data)
		elif code >= 400 and mime_type == "text/plain":
			self["stdout"].write(resp_name.encode())


if __name__ == "__main__":
	import os
	sys.path[0] = os.getcwd()

	from fastcgi import FcgiHandler

	class fcgi_debug_handler(HttpResponseMixin, FcgiHandler):
		def handle(self):
			self.send_response(200, "application/json", self.environ)

	with FcgiServer(fcgi_debug_handler) as server:
		server.serve_forever()



#!/usr/bin/env python3

import sys
import socket
from socketserver import BaseServer


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




if __name__ == "__main__":
	import json
	from fastcgi import FcgiHandler

	class fcgi_debug_handler(FcgiHandler):
		def handle(self):
			self["stdout"].write(b"Content-type: text/json\r\nStatus: 200 OK\r\n\r\n")
			self["stdout"].write(bytes(json.dumps(self.environ, indent = '\t', ensure_ascii = False), "utf-8"))

	with FcgiServer(fcgi_debug_handler) as server:
		server.serve_forever()



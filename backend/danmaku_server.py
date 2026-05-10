#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import socket
import socketserver
import argparse

import constants

class danmaku_handler(socketserver.BaseRequestHandler):
	def handle(self):
		self.request.settimeout(5)
		rid = int(self.request.recv(0x100).decode())
		sock_path = os.path.join(self.server.danmaku_root, str(rid), constants.default_names.danmaku_socket)
		danmaku_sock = None
		pipes = None

		try:
			danmaku_sock = socket.socket(socket.AF_UNIX)
			danmaku_sock.connect(sock_path)
			pipes = os.pipe()

			self.request.settimeout(None)
			while True:
				transferred = os.splice(danmaku_sock.fileno(), pipes[1], 0x10000)
				if transferred == 0:
					break
				transferred = os.splice(pipes[0], self.request.fileno(), 0x10000)
				if transferred == 0:
					break

		finally:
			if danmaku_sock is not None:
				danmaku_sock.close()
			if pipes:
				os.close(pipes[0])
				os.close(pipes[1])


class DanmakuServer(socketserver.ThreadingUnixStreamServer):
	def __init__(self, danmaku_root, *args):
		super().__init__(*args)
		self.danmaku_root = danmaku_root


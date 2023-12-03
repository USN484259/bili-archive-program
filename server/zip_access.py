#!/usr/bin/env python3

import os
import re
import time
import json
import zipfile
import mimetypes
from urllib.parse import parse_qs
from socketserver import ThreadingMixIn
from fcgi_server import FcgiServer
from fastcgi import FcgiHandler

from threading import RLock
from collections import OrderedDict

class lru_cache:
	def __init__(self, func, limit = 16):
		self.cache = OrderedDict()
		self.limit = limit
		self.atime = time.monotonic()
		self.lock = RLock()
		self.new_func = func["new"]
		self.del_func = func["del"]

	def shink(self):
		with self.lock:
			while len(self.cache) > self.limit:
				k, v = self.cache.popitem(0)
				self.del_func(k, v)

	def flush(self):
		with self.lock:
			if len(self.cache) > 0:
				l = self.limit
				self.limit = 0
				self.shink()
				self.limit = l

	def set_limit(self, limit):
		with self.lock:
			self.limit = limit
			self.shink()

	def get_access_time(self):
		return self.atime

	def __call__(self, arg0, *args, **kwargs):
		with self.lock:
			self.atime = time.monotonic()
			if arg0 in self.cache:
				self.cache.move_to_end(arg0)
				return self.cache[arg0]
			else:
				obj = self.new_func(arg0, *args, **kwargs)
				self.cache[arg0] = obj
				self.shink()
				return obj


def copy_stream(dst_stream, src_stream, length, chunk_size = 0x1000):
	count = 0
	while count < length:
		copy_size = min(length - count, chunk_size)
		data = src_stream.read(copy_size)
		if not data:
			break
		dst_stream.write(data)
		count += min(copy_size, len(data))

	return count


class zip_access_handler(FcgiHandler):
	def send_status_404(self, err_str = None):
		self["stdout"].write(b"Content-type: text/plain; charset=UTF-8\r\nStatus: 404 Not Found\r\n\r\n404 Not Found\r\n")
		if err_str:
			self["stdout"].write(bytes(err_str, "utf-8"))

	def send_status_501(self):
		self["stdout"].write(b"Content-Type: text/plain; charset=UTF-8\r\nStatus: 501 Not Implemented\r\n\r\n501 Not Implemented\r\n")

	def send_status_416(self, file_size):
		self["stdout"].write(b"Content-Type: text/plain; charset=UTF-8\r\nStatus: 416 Range Not Satisfiable\r\n")
		range_str = "Content-Range: bytes */%d\r\n\r\n" % file_size
		self["stdout"].write(bytes(range_str, "utf-8"))

	def send_file_status(self, content_type, file_size, range_head, range_tail):

		type_str = "Content-Type: %s\r\n" % content_type
		self["stdout"].write(bytes(type_str, "utf-8"))

		if range_head == 0 and range_tail == file_size:
			self["stdout"].write(b"Status: 200 OK\r\n")
		else:
			self["stdout"].write(b"Status: 206 Partial Content\r\n")
			range_str = "Content-Range: bytes %d-%d/%d\r\n" % (range_head, range_tail - 1, file_size)
			self["stdout"].write(bytes(range_str, "utf-8"))

		length_str = "Content-Length: %d\r\n\r\n" % (range_tail - range_head)
		self["stdout"].write(bytes(length_str, "utf-8"))

	def send_dir_status(self, dir_list):
		self["stdout"].write(b"Content-Type: text/json; charset=UTF-8\r\n\r\n")
		self["stdout"].write(bytes(json.dumps(dir_list, indent = '\t', ensure_ascii = False), "utf-8"))

	def handle_dir(self, archive, path):
		dir_list = []
		for info in archive.infolist():
			dirname, filename = os.path.split(info.filename.rstrip('/'))
			if dirname != path:
				continue

			dir_list.append({
				"name": filename,
				"type": info.is_dir() and "dir" or "file",
				"size": info.file_size,
				"mtime": int(time.mktime(info.date_time + (0, 0, 0)) * 1000),
			})

		if not dir_list:
			self.send_status_404()
		else:
			self.send_dir_status(dir_list)


	def handle_file(self, archive, filename):
		info = archive.getinfo(filename)
		if not info:
			raise FileNotFoundError(filename)

		range_head = 0
		range_tail = info.file_size
		range_str = self.environ.get("HTTP_RANGE")
		if range_str:
			range_match = re.match(r"bytes=(\d*)-(\d*)", range_str)
			try:
				head_str = range_match.group(1)
				tail_str = range_match.group(2)
				if head_str:
					range_head = int(head_str)
					if tail_str:
						range_tail = int(tail_str) + 1

				elif tail_str:
					range_head = info.file_size - int(tail_str)
				else:
					raise ValueError

				if range_head < 0 or range_head >= info.file_size:
					raise ValueError
				if range_tail < 0 or range_tail > info.file_size:
					raise ValueError
				if range_head >= range_tail:
					raise ValueError
			except Exception as e:
				self.send_status_416(info.file_size)
				return

		with archive.open(filename, 'r') as f:
			mime_type = mimetypes.guess_type(filename)[0]

			self.send_file_status(mime_type or "application/octet-stream", info.file_size, range_head, range_tail)

			req_method = self.environ.get("REQUEST_METHOD")
			if req_method == "HEAD":
				return

			f.seek(range_head)
			copy_stream(self["stdout"], f, range_tail - range_head)


	def handle(self):
		global zip_cache
		try:
			req_method = self.environ.get("REQUEST_METHOD")
			if req_method not in ("GET", "HEAD"):
				self.send_status_501()
				return

			www_root = self.environ.get("DOCUMENT_ROOT")
			query = parse_qs(self.environ.get("QUERY_STRING"), strict_parsing = True)
			zip_path = query.get("path")[0]
			zip_member = query.get("member")[0].lstrip("/.")
			if not zip_member:
				zip_member = "/"

			archive = zip_cache(os.path.join(www_root, zip_path), mode = 'r')
			if zip_member.endswith('/'):
				self.handle_dir(archive, zip_member.rstrip('/'))
			else:
				self.handle_file(archive, zip_member)


		except Exception as e:
			self.send_status_404()
			return


def zip_close_func(path, obj):
	obj.close()

zip_cache = lru_cache({
	"new":	zipfile.ZipFile,
	"del":	zip_close_func
})


class FcgiThreadingServer(ThreadingMixIn, FcgiServer):
	def service_actions(self):
		super().service_actions()
		global zip_cache
		cur_time = time.monotonic()
		if cur_time - zip_cache.get_access_time() > 30:
			zip_cache.flush()

with FcgiThreadingServer(zip_access_handler) as server:
	server.serve_forever()


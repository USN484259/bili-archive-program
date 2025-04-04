#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import time
import zipfile
import mimetypes
from urllib.parse import parse_qs
from fcgi_server import FcgiThreadingServer, HttpResponseMixin
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


class zip_access_handler(HttpResponseMixin, FcgiHandler):
	def send_file_status(self, content_type, file_size, range_head, range_tail, f):
		req_method = self.environ.get("REQUEST_METHOD")
		length_str = "Content-Length: %d" % (range_tail - range_head)

		def file_content_gen():
			if req_method == "HEAD":
				return

			f.seek(range_head)
			count = 0
			length = range_tail - range_head
			while count < length:
				copy_size = min(length - count, 0x10000)
				data = f.read(copy_size)
				if not data:
					raise EOFError()
				yield data
				count += min(copy_size, len(data))

		if range_head == 0 and range_tail == file_size:
			return self.send_response(200, mime_type = content_type, extra_headers = (length_str, ), data = file_content_gen)
		else:
			range_str = "Content-Range: bytes %d-%d/%d" % (range_head, range_tail - 1, file_size)
			return self.send_response(206, mime_type = content_type, extra_headers = (length_str, range_str), data = file_content_gen)

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
			return self.send_response(404)
		else:
			return self.send_response(200, mime_type = "application/json", data = dir_list)


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
				range_str = "Content-Range: bytes */%d" % info.file_size
				return self.send_response(416, extra_headers = (range_str, ))

		with archive.open(filename, 'r') as f:
			mime_type = mimetypes.guess_type(filename)[0]

			self.send_file_status(mime_type or "application/octet-stream", info.file_size, range_head, range_tail, f)


	def handle(self):
		try:
			req_method = self.environ.get("REQUEST_METHOD")
			if req_method not in ("GET", "HEAD"):
				return self.send_response(405)

			www_root = self.environ.get("DOCUMENT_ROOT")
			query = parse_qs(self.environ.get("QUERY_STRING"), strict_parsing = True)
			zip_path = query.get("path")[0]
			zip_member = query.get("member")[0].lstrip("/.")
			if not zip_member:
				zip_member = "/"

			archive = self.server.zip_cache(os.path.join(www_root, zip_path), mode = 'r')
			if zip_member.endswith('/'):
				self.handle_dir(archive, zip_member.rstrip('/'))
			else:
				self.handle_file(archive, zip_member)

		except Exception as e:
			return self.send_response(404)


class ZipAccessServer(FcgiThreadingServer):
	def __init__(self, handler):
		super().__init__(handler)

		def zip_close_func(path, obj):
			obj.close()

		self.zip_cache = lru_cache({
			"new":	zipfile.ZipFile,
			"del":	zip_close_func
		})

	def service_actions(self):
		super().service_actions()
		cur_time = time.monotonic()
		if cur_time - self.zip_cache.get_access_time() > 30:
			self.zip_cache.flush()

with ZipAccessServer(zip_access_handler) as server:
	server.serve_forever(poll_interval = 60)


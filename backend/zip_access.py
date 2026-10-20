#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import json
import time
import logging
import zipfile
import mimetypes
import threading
from functools import partial
from collections import OrderedDict
from urllib.parse import parse_qs
from simple_fastcgi import FcgiThreadingServer, HttpResponseMixin, FcgiHandler

from poller import Timer
from utils import logger_init

ZIP_CACHE_LIMIT = 0x40
ZIP_CLOSE_TIMEOUT = 120 * 1000
POLL_INTERVAL_MS = 60 * 1000

logger = logging.getLogger("bili-arch.zip_access")


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


	@staticmethod
	def dir_listing(archive, path, ndjson = False):
		count = 0
		prepend = (not ndjson) and "[\n" or ""
		for info in archive.infolist():
			dirname, filename = os.path.split(info.filename.rstrip('/'))
			if dirname != path:
				continue

			yield "%s%s\n" % (prepend, json.dumps({
				"name": filename,
				"type": info.is_dir() and "dir" or "file",
				"size": info.file_size,
				"mtime": int(time.mktime(info.date_time + (0, 0, 0)) * 1000),
			}, ensure_ascii = False))
			count += 1
			if prepend:
				prepend = ",\n"

		if count == 0:
			raise FileNotFoundError(path)
		if not ndjson:
			yield "]\n"


	def handle_dir(self, archive, path):
		accept = self.environ.get("HTTP_ACCEPT")
		is_ndjson = (accept and "ndjson" in accept)
		try:
			func = partial(self.dir_listing, archive, path, is_ndjson)
			return self.send_response(200, is_ndjson and "application/x-ndjson" or "application/json", data = func)
		except FileNotFoundError:
			return self.send_response(404)


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
			logger.debug("handler on thread %d", threading.get_native_id())
			req_method = self.environ.get("REQUEST_METHOD")
			if req_method not in ("GET", "HEAD"):
				return self.send_response(405)

			www_root = self.environ.get("DOCUMENT_ROOT")
			query = parse_qs(self.environ.get("QUERY_STRING"), strict_parsing = True)
			zip_path = query.get("path")[0].lstrip("/.")
			if ".." in zip_path:
				raise RuntimeError("invalid zip path %s" % zip_path)
			zip_member = query.get("member")[0].lstrip("/.")
			if not zip_member:
				zip_member = "/"

			logger.verbose("path %s, member %s", zip_path, zip_member)

			archive = self.server.get(os.path.join(www_root, zip_path))
			if zip_member.endswith('/'):
				self.handle_dir(archive, zip_member.rstrip('/'))
			else:
				self.handle_file(archive, zip_member)

		except Exception as e:
			logger.notice("error in handle %s %s: %s", zip_path, zip_member, e)
			return self.send_response(404)


class ZipAccessServer(FcgiThreadingServer):
	class Record:
		def __init__(self, path):
			logger.info("opening %s", path)
			self.path = path
			self.archive = zipfile.ZipFile(path, mode = 'r')
			self.atime = time.monotonic()

		def close(self):
			logger.info("closing %s", self.path)
			self.archive.close()

	def __init__(self, handler):
		super().__init__(handler)
		self.lock = threading.RLock()
		self.zip_cache = OrderedDict()
		self.timer = Timer()
		self.add_watch(self.timer, lambda obj: obj.drain())

	def _shrink(self):
		# lock already held
		while len(self.zip_cache) > ZIP_CACHE_LIMIT:
			path, rec = self.zip_cache.popitem(0)
			rec.close()

	def get(self, path):
		with self.lock:
			needs_timer = bool(self.zip_cache)
			rec = self.zip_cache.get(path)
			if rec is not None:
				rec.atime = time.monotonic()
			else:
				rec = Record(path)
				self.zip_cache[path] = rec

			self.zip_cache.move_to_end(path)
			self._shrink()

		if needs_timer:		# previously no open zip file, start timer
			logger.verbose("start timer")
			self.timer.settime(POLL_INTERVAL_MS, repeat = True)
		return rec.archive

	def service_actions(self):
		super().service_actions()
		cur_time = time.monotonic()
		with self.lock:
			while self.zip_cache:
				rec = next(iter(self.zip_cache.values()))
				if rec.atime + ZIP_CLOSE_TIMEOUT >= cur_time:
					return
				path, r = self.zip_cache.popitem(0)
				assert(r is rec)
				rec.close()

		# all zip files closed, cancel timer
		logger.verbose("stop timer")
		self.timer.cancel()

if __name__ == "__main__":
	logger_init()
	with ZipAccessServer(zip_access_handler) as server:
		server.serve_forever()


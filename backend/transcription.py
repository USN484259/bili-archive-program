#!/usr/bin/env python3

# This file is written with the assistance of opencode-deepseek-v4-flash

# FastCGI backend for the live transcription pipeline (the client is
# tools/live_transcribe.py).
#
# Endpoints (all under the FastCGI url, e.g. /api/transcription):
#   GET  -> {path, list, hash, hashing_speed}
#            * list: pending media as doc-root-relative URL paths with a
#              leading '/', e.g. "/live/out12.flv"; clients urljoin these
#              directly to fetch the media and PUT the SRT
#            * hash/hashing_speed: enabled hash methods and the measured
#              MiB/s of the hash worker (used by clients to size their
#              503 retry budget)
#   POST {add|del: [path, ...]}  -> add/remove pending (doc-root-relative)
#   PUT  <media>.srt?size=..[&<hash>=..] -> verify and accept the SRT
#
# SRT naming: "<media> + .srt" (a.flv -> a.flv.srt), stored next to the
# media under live_root. On PUT the size (always) and, when hashes are
# enabled, the hash of the media file must match, so the server only ever
# stores SRTs transcribed from the file it knows about. Hashes are computed
# by a background worker; while a large file is still being hashed the PUT
# answers 503 so the client knows to wait and retry.

import os
import sys
sys.path[0] = os.getcwd()

import re
import json
import time
import signal
import hashlib
import logging
import argparse
import selectors
import threading
from queue import SimpleQueue
from contextlib import suppress
from collections import namedtuple
from urllib.parse import parse_qs, unquote

from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler

watch_mask = None
with suppress(ModuleNotFoundError):
	from simple_inotify import *
	inotify_watch_mask = (IN_CLOSE_WRITE | IN_MODIFY | IN_DELETE_SELF | IN_MOVE_SELF)

from fops import create_unix_socket
from utils import get_relative_path, parse_size, logger_init
from messaging import MessagingClient

# constants

import constants

ns_in_sec = 1000 * 1000 * 1000
bytes_in_MiB = 0x100000
allowed_media_ext = (".flv", ".mp4", ".zip", ".m4a")
srt_max_size = 1 * bytes_in_MiB	# max accepted srt file size
hash_get_timeout = 10		# how long a PUT waits for the hash worker before 503
srt_recv_timeout = 30		# max allowed time to receive an srt file
hashing_measure_file_path = "/dev/urandom"	# speed benchmark source

# static objects

logger = logging.getLogger("bili_arch.transcription")


# helper functions

class HashCache:
	"""Compute and cache media hashes in a background thread.

	Files are watched with inotify so a cache entry is dropped whenever the
	media changes. The worker runs at a low nice/ionice priority and, at
	startup, measures its own hashing throughput; GET exposes that speed as
	"hashing_speed" so transcription clients can estimate how long to keep
	retrying a 503 (hash still in progress) before it can possibly be done."""

	hash_kwargs = (sys.hexversion >= 0x03090000) and {"usedforsecurity": False} or {}
	chunk_size = 0x100000
	queue_max_size = 0x1000
	Record = namedtuple("Record", ("wd", "size", "hash"))

	def __init__(self, hashes, *, nice = None):
		self.hash_methods = set(hashes)
		self.nice_value = nice or 10
		self.hashing_speed = None
		self.observer = Inotify(IN_NONBLOCK | IN_CLOEXEC)
		self.cache = {}
		self.thread = threading.Thread(target = self.hash_worker, daemon = False)
		self.cond = threading.Condition()
		self.queue = SimpleQueue()
		self.quit = False
		self.thread.start()


	def fileno(self):
		return self.observer.fileno()


	def clear(self):
		logger.info("clear hash cache")
		with self.cond:
			old_cache = self.cache
			self.cache = {}
			self.cond.notify_all()

		for path, rec in old_cache.items():
			with suppress(OSError):
				self.observer.rm_watch(rec.wd)

	def close(self):
		logger.debug("HashCache exiting")
		self.quit = True
		self.queue.put_nowait("")
		with self.cond:
			self.cond.notify_all()
		self.thread.join()


	def update(self):
		events = self.observer.read()
		for ev in events:
			if not (ev.mask & inotify_watch_mask):
				continue

			path = self.observer.get(ev.wd)
			if not path:
				continue
			logger.debug("dropping hash cache for %s", path)
			self.drop(path)


	def methods(self):
		return list(self.hash_methods)


	def pick_method(self, keys):
		result = set(keys) & self.hash_methods
		if not result:
			return None
		return result.pop()


	def push(self, path):
		path = os.path.normpath(path)
		logger.info("add media file %s", path)
		with self.cond:
			if path in self.cache:
				return True

		wd = None
		try:
			stat = os.stat(path)
			size = stat.st_size
			wd = self.observer.add_watch(path, inotify_watch_mask)
		except OSError as e:
			logger.warning("cannot watch on media %s: %s", path, str(e))
			return False

		with self.cond:
			self.cache[path] = self.Record(wd, size, {})


		queue_size = self.queue.qsize()
		if queue_size >= self.queue_max_size:
			logger.warning("queue overflow %d, dropping %s", queue_size, path)
			self.drop(path)
			return False
		else:
			self.queue.put_nowait(path)
			return True


	def drop(self, path):
		path = os.path.normpath(path)
		logger.info("del media file %s", path)
		with self.cond:
			rec = self.cache.pop(path, None)
			self.cond.notify_all()

		if not rec:
			logger.warning("cannot find record for %s", path)
			return False
		with suppress(OSError):
			self.observer.rm_watch(rec.wd)
		return True

	def get(self, path, hash_name, *, timeout = None):
		if hash_name not in self.hash_methods:
			return None

		path = os.path.normpath(path)
		expire_time = timeout and time.monotonic() + timeout

		with self.cond:
			rec = self.cache.get(path)

		if not rec:
			self.push(path)

		while not self.quit:
			if rec and rec.hash:
				hash_obj = rec.hash.get(hash_name)
				if hash_obj:
					return hash_obj.hexdigest().lower()

			if expire_time:
				wait_time = expire_time - time.monotonic()
				if wait_time <= 0:
					raise TimeoutError()
			else:
				wait_time = None

			with self.cond:
				self.cond.wait(wait_time)
				rec = self.cache.get(path)

			if not rec:
				return None

		logger.warning("quitting hash-get for %s", path)
		return None

	def get_hashing_speed(self):
		return self.hashing_speed

	def measure_hashing_speed(self, duration = 2):
		try:
			with open(hashing_measure_file_path, mode = "rb") as f:
				record = { name: hashlib.new(name, **self.hash_kwargs) for name in self.hash_methods }
				duration_ns = duration * ns_in_sec
				size = 0
				cur_time = None
				start_time = time.monotonic_ns()
				logger.debug("measure hashing speed for %d seconds, start %d", duration, start_time)
				while (cur_time is None) or (cur_time - start_time < duration_ns):
					data = f.read(self.chunk_size)
					if not data:
						break
					size += len(data)
					for hash_func in record.values():
						hash_func.update(data)
					cur_time = time.monotonic_ns()

			logger.debug("measure hashing speed done, size %d, stop %d", size, cur_time)

			raw_hashing_speed = size * ns_in_sec // (cur_time - start_time)
			self.hashing_speed = raw_hashing_speed // bytes_in_MiB
			logger.info("hashing speed %d MiB/s (%d)", self.hashing_speed, raw_hashing_speed)

		except Exception:
			logger.exception("failed in measuring hashing speed")


	def hash_worker(self):
		logger.debug("HashCache worker started")
		try:
			import psutil
			proc = psutil.Process(threading.get_native_id())
			proc.nice(self.nice_value)
			proc.ionice(psutil.IOPRIO_CLASS_IDLE)
		except Exception as e:
			logger.warning("cannot set IO priority: %s", str(e))

		self.measure_hashing_speed()

		while not self.quit:
			path = self.queue.get()
			if self.quit:
				break
			logger.debug("hash worker got %s", path)

			with self.cond:
				rec = self.cache.get(path)
				if not rec:
					self.cond.notify_all()
					continue

			record = None
			size = 0
			try:
				with open(path, "rb") as f:
					record = { name: hashlib.new(name, **self.hash_kwargs) for name in self.hash_methods }
					logger.debug("calculating %d hashes for %s", len(record), path)

					while True:
						data = f.read(self.chunk_size)
						if not data:
							break
						size += len(data)
						for hash_func in record.values():
							hash_func.update(data)

				for name, func in record.items():
					logger.debug("%s\t%s", name, func.hexdigest())

			except FileNotFoundError:
				self.drop(path)
				continue

			except Exception:
				logger.exception("exception in calculating hashes for %s", path)
				record = None

			with self.cond:
				rec = self.cache.get(path)
				if rec:
					if record and size == rec.size:
						for k, v in record.items():
							rec.hash[k] = v
					else:
						# Condition defaults to RLock, allows reentrant
						self.drop(path)

				self.cond.notify_all()

		logger.debug("HashCache worker exit")


# classes

class transcription_handler(HttpResponseMixin, FcgiHandler):
	def receive_file(self, path, upload_size):
		start_time = time.monotonic()
		payload = bytearray()
		while len(payload) < upload_size:
			data = self.read(upload_size)
			if not data:
				logger.warning("unexpected eof at %d for %s", len(payload), path)
				return self.send_response(413)
			payload += data
			if time.monotonic() - start_time >= srt_recv_timeout:
				logger.warning("receive %s timeout", path)
				return self.send_response(408)

		if len(payload) != upload_size:
			logger.warning("invalid file size %d for %s", len(payload), path)
			return self.send_response(413)

		os.makedirs(os.path.dirname(path), exist_ok = True)

		try:
			with open(path, mode = "xb") as f:
				f.write(payload)
			logger.info("saved to %s, size %d", path, len(payload))
			return self.send_response(201)
		except FileExistsError:
			file_size = "?"
			with suppress(OSError):
				stat = os.stat(path)
				file_size = str(stat.st_size)

			logger.warning("file exists %s, size %d/%s", path, len(payload), file_size)
			return self.send_response(409)

	def handle_put(self):
		upload_size = self.environ.get("CONTENT_LENGTH")
		if not upload_size:
			return self.send_response(411)
		try:
			upload_size = int(upload_size)
		except ValueError:
			return self.send_response(411)

		if upload_size <= 0 or upload_size > srt_max_size:
			return self.send_response(413)

		doc_root = self.environ.get("DOCUMENT_ROOT")
		req_uri = self.environ.get("REQUEST_URI")
		try:
			req_path = unquote(req_uri.lstrip('/.').split('?', 1)[0], errors = 'strict')
		except UnicodeError:
			return self.send_response(400)
		if ".." in req_path:
			return self.send_response(403)

		# the client proves it transcribed THIS file by passing the exact
		# media size (always) and, when hashes are enabled, the expected hash
		query = parse_qs(self.environ.get("QUERY_STRING"), strict_parsing = True)
		hash_name = None
		hash_value = None
		file_size = None

		try:
			file_size = int(query["size"][0])
		except (KeyError, IndexError, ValueError):
			logger.warning("missing/invalid size for PUT %s", req_path)
			return self.send_response(400)

		if self.server.hash_cache is not None:
			hash_name = self.server.hash_cache.pick_method(query.keys())
			if not hash_name:
				return self.send_response(400)
			hash_value = query[hash_name][0]

		path = os.path.join(doc_root, req_path)
		rel_path = get_relative_path(path, self.server.live_root)
		logger.debug("path %s, using %s", path, hash_name or "file_size")
		if not rel_path:
			logger.warning("PUT outside live root: %s", path)
			return self.send_response(403)

		(media_file, srt_ext) = os.path.splitext(path)
		if srt_ext != ".srt":
			return self.send_response(415)

		if os.access(path, os.F_OK):
			logger.warning("file exists %s", path)
			return self.send_response(409)

		media_ext = os.path.splitext(media_file)[1]
		if media_ext not in allowed_media_ext:
			logger.warning("unknown media type %s", media_ext)
			return self.send_response(415)

		try:
			stat = os.stat(media_file)
			logger.debug("file %s, size %d", media_file, stat.st_size)
			if stat.st_size != file_size:
				logger.warning("size mismatch for %s: %d != %d", media_file, stat.st_size, file_size)
				return self.send_response(400)
		except OSError:
			logger.warning("media not found: %s", media_file)
			return self.send_response(404)

		if self.server.hash_cache is not None:
			# the hash may not be ready yet (large file still being hashed);
			# 503 tells the client to wait and retry the PUT
			try:
				file_hash = self.server.hash_cache.get(media_file, hash_name, timeout = hash_get_timeout)
			except TimeoutError:
				return self.send_response(503)

			if not file_hash:
				logger.warning("hash missing for %s", media_file)
				return self.send_response(404)
			logger.debug("file %s, %s %s", media_file, hash_name, file_hash)
			if file_hash != hash_value.lower():
				logger.warning("hash mismatch for %s", media_file)
				return self.send_response(400)

		out_path = os.path.join(self.server.out_path, rel_path)
		logger.info("accepting %s for media %s, size %d", rel_path, media_file, upload_size)
		return self.receive_file(out_path, upload_size)


	def handle_get(self):
		doc_root = self.environ.get("DOCUMENT_ROOT")
		# "list" holds doc-root-relative URL paths (leading '/') so clients
		# can urljoin them directly to fetch the media and PUT the SRT;
		# "path" is the URL prefix of live_root under doc_root (compat)
		result = {
			"path": '/' + get_relative_path(self.server.live_root, doc_root),
			"list": self.server.get_media_list(doc_root),
			"hash": self.server.hash_cache and self.server.hash_cache.methods(),
			"hashing_speed": self.server.hash_cache and self.server.hash_cache.get_hashing_speed()
		}
		return self.send_response(200, json = result)


	def handle_post(self):
		# {add|del: [doc-root-relative paths, matching the GET "list" items]}
		# lets a client update the pending set without restarting the server
		try:
			info = json.loads(self.read().decode())
		except (UnicodeError, ValueError):
			return self.send_response(400)

		add_list = info.pop("add", None)
		del_list = info.pop("del", None)

		if ((add_list is None or isinstance(add_list, list))
		and (del_list is None or isinstance(del_list, list))
		and (add_list or del_list) and not info):
			pass
		else:
			return self.send_response(418)

		def process_items(item_list, func):
			doc_root = self.environ.get("DOCUMENT_ROOT")
			for item in item_list:
				if not isinstance(item, str) or ".." in item:
					logger.warning("invalid path %s", str(item))
					continue

				path = os.path.join(doc_root, item.lstrip("/."))

				media_ext = os.path.splitext(path)[1]
				if media_ext not in allowed_media_ext:
					logger.warning("unknown media type %s", media_ext)
					continue

				func(path)

		if add_list:
			process_items(add_list, self.server.add_media_file)

		if del_list:
			process_items(del_list, self.server.del_media_file)

		return self.send_response(200)


	def handle(self):
		try:
			req_method = self.environ.get("REQUEST_METHOD")
			# per-request trace; the notable events (PUT accept, POST, walks)
			# are logged individually at INFO below
			logger.debug("%s %s", req_method, self.environ.get("REQUEST_URI"))

			if req_method in ("GET", "HEAD"):
				return self.handle_get()
			elif req_method == "POST":
				return self.handle_post()
			elif req_method == "PUT":
				return self.handle_put()
			else:
				return self.send_response(405)

		except Exception as e:
			logger.exception("exception in handle request: %s", str(e))
			return self.send_response(500)


class TranscriptionServer(FcgiServer):
	def __init__(self, handler, socket, args):
		super().__init__(handler, socket)
		self.live_root = os.path.normpath(args.path)
		self.out_path = args.out or self.live_root
		self.watch_paths = set()
		self.filter_path = args.filter
		self.matches = []
		self.msg_client = MessagingClient(args.msg_addr)
		self.hash_cache = None
		self._wake_r, self._wake_w = os.pipe()
		self.record = set()
		self.reload = False
		self.clear = False
		self.quit = False

		os.set_blocking(self._wake_r, False)
		os.set_blocking(self._wake_w, False)
		if args.hash:
			logger.debug("using hash %s", ' '.join(args.hash))
			self.hash_cache = HashCache(args.hash)

		logger.debug("live-root %s, out-path %s", self.live_root, self.out_path)
		self.watch_paths.add(self.live_root)
		for path in (args.watch or ()):
			logger.debug("watching %s", path)
			self.watch_paths.add(os.path.normpath(path))

		self.reload_filter()
		self.msg_client.subscribe(constants.topic.live_rec, constants.topic.live_monitor)

	def reload_filter(self):
		if not self.filter_path:
			return
		try:
			with open(self.filter_path, mode = "rt") as f:
				data = f.read()
			filter_list = tuple(filter(lambda l: l and l[0] != '#', data.split('\n')))
			logger.debug("filters (%s)", ") | (".join(filter_list))
			self.matches = [re.compile(l) for l in filter_list]
			logger.info("loaded %d filters", len(self.matches))
		except Exception as e:
			logger.error("cannot load filter from %s: %s", self.filter_path, str(e))


	def _wake(self):
		with suppress(OSError):
			os.write(self._wake_w, b"x")

	# override
	def server_close(self):
		self.quit = True
		self._wake()

		with suppress(OSError):
			os.close(self._wake_r)
		self._wake_r = -1
		with suppress(OSError):
			os.close(self._wake_w)
		self._wake_w = -1
		try:
			self.msg_client.close()
			if self.hash_cache:
				self.hash_cache.close()
		finally:
			super().server_close()


	def on_signal(self, signum, frame):
		if signum == signal.SIGUSR1:
			self.reload = True
		elif signum == signal.SIGUSR2:
			self.clear = True
		else:
			self.quit = True
			signal.signal(signum, signal.SIG_DFL)
		self._wake()


	# returns the pending media as doc-root-relative URL paths with a leading
	# '/', e.g. "/live/out12.flv"; clients feed these straight into
	# urljoin() / PUT / POST
	def get_media_list(self, doc_root):
		result = []
		for path in self.record:
			rel_path = get_relative_path(path, doc_root)
			if rel_path:
				result.append('/' + rel_path)
		return result


	# expects absolute normalized path
	def add_media_file(self, path):
		self.record.add(path)
		if self.hash_cache is not None:
			self.hash_cache.push(path)


	# expects absolute normalized path
	def del_media_file(self, path):
		self.record.discard(path)
		if self.hash_cache is not None:
			self.hash_cache.drop(path)


	def handle_message(self):
		# a live recorder announced "record-stopped"; find the media files in
		# that folder that still lack an SRT and put them on the pending list
		self.msg_client.wait(0)
		while True:
			topic, info = self.msg_client.recv(constants.topic.live_rec, constants.topic.live_monitor)
			if not topic:
				break
			if not isinstance(info, dict) or info.get("event", "") != "record-stopped":
				continue
			msg_path = info.get("path")
			logger.info("got record folder %s", msg_path)
			rel_path = None
			for path in self.watch_paths:
				rel_path = get_relative_path(msg_path, path)
				if rel_path:
					break
			else:
				logger.info("skip folder %s", msg_path)
				continue

			if self.matches:
				name = os.path.basename(rel_path)
				logger.debug("name %s, matching %d filters", name, len(self.matches))
				for obj in self.matches:
					if obj.match(name):
						break
				else:
					logger.info("filtered %s", rel_path)
					continue

			path = os.path.join(self.live_root, rel_path)
			try:
				if not os.path.isdir(path):
					continue
			except OSError:
				continue

			logger.info("walking record folder %s", path)
			# walk the folder
			with os.scandir(path) as it:
				for entry in it:
					logger.debug(entry.name)
					if not entry.is_file():
						continue
					if os.path.splitext(entry.name)[1] not in allowed_media_ext:
						continue
					if os.access(entry.path + ".srt", os.F_OK):
						continue
					self.add_media_file(entry.path)


	def run(self):
		logger.info("transcription server started")
		with selectors.DefaultSelector() as sel:
			sel.register(self, selectors.EVENT_READ)
			sel.register(self._wake_r, selectors.EVENT_READ)
			sel.register(self.msg_client, selectors.EVENT_READ)
			if self.hash_cache is not None:
				sel.register(self.hash_cache, selectors.EVENT_READ)

			while not self.quit:
				results = sel.select()
				if self.quit:
					break
				if self.clear:
					self.clear = False
					if self.hash_cache is not None:
						self.hash_cache.clear()
				if self.reload:
					self.reload = False
					self.reload_filter()

				for key, ev in results:
					try:
						if key.fileobj is self:
							self._handle_request_noblock()
						elif key.fileobj is self._wake_r:
							with suppress(OSError):
								while os.read(self._wake_r, 0x1000):
									pass
						elif key.fileobj is self.msg_client:
							self.handle_message()
						elif key.fileobj is self.hash_cache:
							self.hash_cache.update()

					except Exception as e:
						logger.exception("exception in server loop")


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--hash", nargs = '*')
	parser.add_argument("-s", "--socket")
	parser.add_argument("--msg-addr")
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("--watch", nargs = '*', action = "extend")
	parser.add_argument("--filter")
	parser.add_argument("--out")
	parser.add_argument("path")

	args = parser.parse_args()
	logger_init(args.verbose)

	logger.info("live-root %s, msg-addr %s, socket %s", args.path, args.msg_addr, args.socket)

	socket = args.socket and create_unix_socket(args.socket, mode = 0o660)

	with TranscriptionServer(transcription_handler, socket, args) as server:
		signal.signal(signal.SIGUSR1, server.on_signal)
		signal.signal(signal.SIGUSR2, server.on_signal)
		signal.signal(signal.SIGTERM, server.on_signal)
		signal.signal(signal.SIGINT, server.on_signal)
		server.run()

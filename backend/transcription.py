#!/usr/bin/env python3

# This file is written with the assistance of opencode-deepseek-v4-flash and opencode-deepseek-v4.1-flash

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
import threading
from stat import S_ISDIR
from queue import SimpleQueue
from contextlib import suppress
from collections import namedtuple, OrderedDict
from urllib.parse import parse_qs, unquote

from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler

Inotify = None
with suppress(ModuleNotFoundError):
	from simple_inotify import Inotify
	from simple_inotify.constants import *

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
		logger.debug("creating HashCache %s", ' '.join(hashes))
		self.hash_methods = set(hashes)
		self.nice_value = nice or 10
		self.hashing_speed = None
		self.observer = Inotify(IN_NONBLOCK | IN_CLOEXEC)
		self.watch_mask = (IN_CLOSE_WRITE | IN_MODIFY | IN_DELETE_SELF | IN_MOVE_SELF)
		self.cache = {}
		self.thread = threading.Thread(target = self.hash_worker, daemon = False)
		self.cond = threading.Condition()
		self.queue = SimpleQueue()
		self.quit = False
		self.thread.start()
		logger.debug("HashCache nice %d, fd %d, tid %d", self.nice_value, self.fileno(), self.thread.native_id)


	def fileno(self):
		return self.observer.fileno()


	def clear(self):
		logger.verbose("clear hash cache")
		with self.cond:
			old_cache = self.cache
			self.cache = {}
			self.cond.notify_all()

		for path, rec in old_cache.items():
			logger.trace("%s %s", path, rec)
			with suppress(OSError):
				self.observer.rm_watch(rec.wd)

	def close(self):
		logger.debug("closing HashCache")
		self.quit = True
		self.queue.put_nowait("")
		with self.cond:
			self.cond.notify_all()
		self.thread.join()
		logger.debug("closed HashCache")


	def update(self):
		events = self.observer.read()
		for ev in events:
			logger.trace(ev)
			if not (ev.mask & self.watch_mask):
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
		logger.debug("requesting %s, matching %s", ' '.join(keys), ' '.join(result))
		if not result:
			return None
		return result.pop()


	def push(self, path):
		path = os.path.normpath(path)
		logger.verbose("add media file %s", path)
		with self.cond:
			if path in self.cache:
				logger.debug("hash exists for %s", path)
				return True

		wd = None
		try:
			stat = os.stat(path)
			logger.trace(stat)
			size = stat.st_size
			if os.access(path + ".srt", os.F_OK):
				logger.notice("srt exists for %s, refused", path)
				return False
			wd = self.observer.add_watch(path, self.watch_mask)
		except OSError as e:
			logger.warning("cannot watch on media %s: %s", path, str(e))
			return False

		logger.debug("push %s, size %d, wd %d", path, size, wd)
		with self.cond:
			self.cache[path] = self.Record(wd, size, {})


		queue_size = self.queue.qsize()
		logger.debug("queue size %d", queue_size)
		if queue_size >= self.queue_max_size:
			logger.warning("queue overflow %d, dropping %s", queue_size, path)
			self.drop(path)
			return False
		else:
			self.queue.put_nowait(path)
			return True


	def drop(self, path):
		path = os.path.normpath(path)
		logger.verbose("del media file %s", path)
		with self.cond:
			rec = self.cache.pop(path, None)
			self.cond.notify_all()

		if not rec:
			logger.notice("cannot find record for %s", path)
			return False

		logger.debug("drop %s, size %d, wd %d", path, rec.size, rec.wd)
		with suppress(OSError):
			self.observer.rm_watch(rec.wd)
		return True


	def get(self, path, hash_name, *, timeout = None):
		if hash_name not in self.hash_methods:
			logger.notice("unsuppported hash %s", hash_name)
			return None

		path = os.path.normpath(path)
		expire_time = timeout and time.monotonic() + timeout
		logger.debug("get %s hash %s, expire %s", path, hash_name, expire_time)

		with self.cond:
			rec = self.cache.get(path)

		if not rec:
			self.push(path)

		while not self.quit:
			if rec and rec.hash:
				hash_obj = rec.hash.get(hash_name)
				if hash_obj:
					return hash_obj.hexdigest().lower()
				else:
					logger.warning("missing hash %s for %s", hash_name, path)
					return None

			if expire_time:
				wait_time = expire_time - time.monotonic()
				logger.debug("wait %f", wait_time)
				if wait_time <= 0:
					raise TimeoutError()
			else:
				wait_time = None

			with self.cond:
				logger.debug("waiting hash for %s", path)
				self.cond.wait(wait_time)
				rec = self.cache.get(path)

			if not rec:
				logger.debug("hash dropped for %s", path)
				return None

		logger.notice("quitting hash-get for %s", path)
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
			logger.verbose("hashing speed %d MiB/s (%d)", self.hashing_speed, raw_hashing_speed)

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
			logger.notice("cannot set worker thread priority: %s", str(e))

		self.measure_hashing_speed()

		while not self.quit:
			path = self.queue.get()
			if self.quit:
				break
			logger.debug("hash worker got %s", path)

			with self.cond:
				rec = self.cache.get(path)
				if not rec:
					logger.debug("hash dropped for %s, notify all", path)
					self.cond.notify_all()
					continue

			record = None
			size = 0
			try:
				with open(path, "rb") as f:
					record = { name: hashlib.new(name, **self.hash_kwargs) for name in self.hash_methods }
					logger.verbose("calculating %d hashes for %s", len(record), path)

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
				logger.notice("file not exist %s, dropped", path)
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
						logger.notice("size changing %s, dropped", path)
						# Condition defaults to RLock, allows reentrant
						self.drop(path)

				self.cond.notify_all()

		logger.debug("HashCache worker exit")


# classes

class transcription_handler(HttpResponseMixin, FcgiHandler):
	def receive_file(self, path, upload_size):
		logger.debug("recv file to %s, size %d", path, upload_size)
		start_time = time.monotonic()
		payload = bytearray()
		while len(payload) < upload_size:
			data = self.read(upload_size)
			if not data:
				logger.notice("unexpected eof at %d for %s", len(payload), path)
				return self.send_response(413)
			payload += data
			if time.monotonic() - start_time >= srt_recv_timeout:
				logger.notice("receive %s timeout", path)
				return self.send_response(408)

		if len(payload) != upload_size:
			logger.notice("invalid file size %d for %s", len(payload), path)
			return self.send_response(413)

		dir_path = os.path.dirname(path)
		logger.debug("creating dir %s", dir_path)
		os.makedirs(dir_path, exist_ok = True)

		# trying to preserve dir mtime
		dir_stat = None
		with suppress(OSError):
			dir_stat = os.stat(dir_path)
			logger.debug("dir %s, mode %o, times %d/%d", dir_path, dir_stat.st_mode, dir_stat.st_atime_ns, dir_stat.st_mtime_ns)
			logger.debug(dir_stat)
			if not S_ISDIR(dir_stat.st_mode):
				dir_stat = None

		try:
			with open(path, mode = "xb") as f:
				if dir_stat:
					try:
						os.utime(dir_path, ns = (dir_stat.st_atime_ns, dir_stat.st_mtime_ns))
					except OSError as e:
						logger.warning("cannot restore dir times %d/%d for %s: %s", dir_stat.st_atime_ns, dir_stat.st_mtime_ns, dir_path, str(e))

				f.write(payload)

			logger.info("saved to %s, size %d", path, len(payload))
			return self.send_response(201)
		except FileExistsError:
			file_size = "?"
			with suppress(OSError):
				stat = os.stat(path)
				logger.trace(stat)
				file_size = str(stat.st_size)

			logger.warning("file exists %s, size %d/%s", path, len(payload), file_size)
			return self.send_response(409)

	def handle_put(self):
		upload_size = self.environ.get("CONTENT_LENGTH")
		logger.debug("content length %s", upload_size)
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
		logger.trace(query)
		hash_name = None
		hash_value = None
		file_size = None

		try:
			file_size = int(query["size"][0])
		except (KeyError, IndexError, ValueError):
			logger.notice("missing/invalid size for PUT %s", req_path)
			return self.send_response(400)

		if self.server.hash_cache is not None:
			hash_name = self.server.hash_cache.pick_method(query.keys())
			if not hash_name:
				logger.notice("invalid hashing method for PUT %s", req_path)
				return self.send_response(400)
			hash_value = query[hash_name][0]

		path = os.path.join(doc_root, req_path)
		rel_path = get_relative_path(path, self.server.live_root)
		logger.verbose("path %s, using %s", path, hash_name or "file_size")
		if not rel_path:
			logger.notice("PUT outside live root: %s", path)
			return self.send_response(403)

		(media_file, srt_ext) = os.path.splitext(path)
		if srt_ext != ".srt":
			logger.notice("invalid file extension %s", srt_ext)
			return self.send_response(415)

		if os.access(path, os.F_OK):
			logger.notice("file exists %s", path)
			return self.send_response(409)

		media_ext = os.path.splitext(media_file)[1]
		if media_ext not in allowed_media_ext:
			logger.warning("unknown media type %s", media_ext)
			return self.send_response(415)

		try:
			stat = os.stat(media_file)
			logger.verbose("media %s, size %d", media_file, stat.st_size)
			if stat.st_size != file_size:
				logger.notice("size mismatch for %s: %d/%d", media_file, stat.st_size, file_size)
				return self.send_response(400)
		except OSError:
			logger.notice("media not found: %s", media_file)
			return self.send_response(404)

		if self.server.hash_cache is not None:
			# the hash may not be ready yet (large file still being hashed);
			# 503 tells the client to wait and retry the PUT
			try:
				file_hash = self.server.hash_cache.get(media_file, hash_name, timeout = hash_get_timeout)
			except TimeoutError:
				logger.notice("hash get timeout for %s", media_file)
				return self.send_response(503)

			if not file_hash:
				logger.notice("hash missing for %s", media_file)
				return self.send_response(404)
			logger.verbose("media %s, %s %s", media_file, hash_name, file_hash)
			if file_hash != hash_value.lower():
				logger.notice("hash mismatch for %s", media_file)
				return self.send_response(400)

		out_path = os.path.join(self.server.out_path, rel_path)
		logger.info("accepting %s for media %s, size %d", rel_path, media_file, upload_size)
		return self.receive_file(out_path, upload_size)


	def handle_get(self):
		doc_root = self.environ.get("DOCUMENT_ROOT")
		# "pending" and "completed" holds doc-root-relative URL paths (leading '/')
		# so clients can urljoin them directly to fetch the media and PUT the SRT;
		# "path" is the URL prefix of live_root under doc_root (compat)
		(pending, completed) = self.server.get_media_list(doc_root)
		result = {
			"path": '/' + get_relative_path(self.server.live_root, doc_root),
			"pending": pending,
			"completed": completed,
			"hash": self.server.hash_cache and self.server.hash_cache.methods(),
			"hashing_speed": self.server.hash_cache and self.server.hash_cache.get_hashing_speed()
		}
		logger.trace(result)
		return self.send_response(200, json = result)


	def handle_post(self):
		# {add|del|done: [doc-root-relative paths, matching the GET "list" items]}
		# lets a client update the pending/completed set without restarting the server
		try:
			info = json.loads(self.read().decode())
			logger.trace(info)
			if not isinstance(info, dict):
				raise TypeError("info is %s", type(info))
		except (UnicodeError, ValueError, TypeError) as e:
			logger.verbose("error in POST: %s", e)
			return self.send_response(400)

		add_list = info.pop("add", None)
		del_list = info.pop("del", None)
		done_list = info.pop("done", None)

		if ((add_list is None or isinstance(add_list, list))
		and (del_list is None or isinstance(del_list, list))
		and (done_list is None or isinstance(done_list, list))
		and (add_list or del_list or done_list) and not info):
			pass
		else:
			logger.notice("no valid action in POST")
			return self.send_response(418)

		def process_items(item_list, func):
			doc_root = self.environ.get("DOCUMENT_ROOT")
			for item in item_list:
				logger.debug(item)
				if not isinstance(item, str) or ".." in item:
					logger.notice("invalid path %s", str(item))
					continue

				path = os.path.join(doc_root, item.lstrip("/."))

				media_ext = os.path.splitext(path)[1]
				if media_ext not in allowed_media_ext:
					logger.notice("unknown media type %s", media_ext)
					continue

				func(path)

		if add_list:
			logger.debug("processing add list %d", len(add_list))
			process_items(add_list, self.server.add_media_file)

		if del_list:
			logger.debug("processing del list %d", len(del_list))
			process_items(del_list, self.server.del_media_file)

		if done_list:
			logger.debug("processing done list", len(done_list))
			process_items(done_list, self.server.done_media_file)

		return self.send_response(200)


	def handle(self):
		try:
			req_method = self.environ.get("REQUEST_METHOD")
			# per-request trace; the notable events (PUT accept, POST, walks)
			# are logged individually at INFO below
			logger.verbose("%s %s", req_method, self.environ.get("REQUEST_URI"))

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
		logger.debug("creating %s", type(self).__name__)
		super().__init__(handler, socket)
		self.live_root = os.path.normpath(args.path)
		self.out_path = args.out or self.live_root
		self.watch_paths = set()
		self.filter_path = args.filter
		self.matches = []
		self.msg_client = MessagingClient(args.msg_addr, reconnect = 1)
		self.hash_cache = None
		self.pending_rec = OrderedDict()
		self.completed_rec = OrderedDict()
		self.reload = False
		self.clear = False
		self.quit = False

		logger.info("live-root %s, out-path %s", self.live_root, self.out_path)
		if args.hash:
			if Inotify is None:
				logger.warning("inotify not available, media hashing disabled")
			else:
				logger.info("using hash %s", ' '.join(args.hash))
				self.hash_cache = HashCache(args.hash)
				self.add_watch(self.hash_cache, self.handle_hash)

		self.watch_paths.add(self.live_root)
		for path in (args.watch or ()):		# watch_paths for bind mounts
			logger.info("watching %s", path)
			self.watch_paths.add(os.path.normpath(path))

		self.reload_filter()
		self.msg_client.subscribe(constants.topic.live_rec, constants.topic.live_monitor)
		self.add_watch(self.msg_client, self.handle_message)


	def reload_filter(self):
		if not self.filter_path:
			return

		logger.debug("loading filter %s", self.filter_path)
		try:
			with open(self.filter_path, mode = "rt") as f:
				data = f.read()
			filter_list = tuple(filter(lambda l: l and l[0] != '#', data.split('\n')))
			logger.verbose("filters (%s)", ") | (".join(filter_list))
			self.matches = [re.compile(l) for l in filter_list]
			logger.info("loaded %d filters", len(self.matches))
		except Exception as e:
			logger.error("cannot load filter from %s: %s", self.filter_path, str(e))


	# override
	def server_close(self):
		super().server_close()
		self.msg_client.close()
		if self.hash_cache:
			self.hash_cache.close()

		logger.debug("closed %s", type(self).__name__)


	def on_signal(self, signum, frame):
		logger.debug("got signal %d", signum)
		if signum == signal.SIGUSR1:
			self.reload = True
		elif signum == signal.SIGUSR2:
			self.clear = True
		else:
			self.quit = True
			signal.signal(signum, signal.SIG_DFL)

		self.wakeup()


	# returns the pending and completed media as doc-root-relative URL paths with a
	# leading '/', e.g. "/live/out12.flv"; clients feed these straight into
	# urljoin() / PUT / POST
	def get_media_list(self, doc_root):
		def make_list(rec):
			result = []
			for path in rec.keys():
				rel_path = get_relative_path(path, doc_root)
				if rel_path:
					result.append('/' + rel_path)
			return result

		result = tuple(make_list(rec) for rec in (self.pending_rec, self.completed_rec))
		logger.trace(result)
		return result


	# expects absolute normalized path
	def add_media_file(self, path):
		if self.done_media_file(path):
			return True

		logger.debug("add media %s", path)
		if self.hash_cache is not None:
			if not self.hash_cache.push(path):
				return False

		self.pending_rec[path] = True
		self.pending_rec.move_to_end(path)
		self.completed_rec.pop(path, None)
		return True


	# expects absolute normalized path
	def done_media_file(self, path):
		logger.debug("done media %s", path)
		if not os.access(path + ".srt", os.F_OK):
			return False

		self.completed_rec[path] = True
		self.completed_rec.move_to_end(path)
		self.pending_rec.pop(path, None)
		if self.hash_cache is not None:
			self.hash_cache.drop(path)
		return True


	# expects absolute normalized path
	def del_media_file(self, path):
		logger.debug("del media %s", path)
		self.completed_rec.pop(path, None)
		self.pending_rec.pop(path, None)
		if self.hash_cache is not None:
			if not self.hash_cache.drop(path):
				return False
		return True


	def handle_hash(self, obj):
		assert(obj is self.hash_cache)
		# serve_forever() does not guard watch callbacks, so an exception here
		# must not be allowed to take down the whole service
		try:
			logger.debug("handle hash")
			self.hash_cache.update()
		except Exception:
			logger.exception("exception in hash cache update")


	def handle_message(self, obj):
		assert(obj is self.msg_client)
		try:
			logger.debug("handle message")
			self.msg_client.wait(0)
			while True:
				topic, info = self.msg_client.recv(constants.topic.live_rec, constants.topic.live_monitor)
				logger.trace("%s %s", topic, info)
				if not topic:
					break
				self.process_message(topic, info)

		except Exception:
			logger.exception("exception in handling message")


	def process_message(self, topic, info):
		# a live recorder announced "record-stopped"; find the media files in
		# that folder that still lack an SRT and put them on the pending list
		if not isinstance(info, dict) or info.get("event", "") != "record-stopped":
			return
		msg_path = info.get("path")
		logger.info("got record folder %s", msg_path)
		rel_path = None
		for path in self.watch_paths:
			rel_path = get_relative_path(msg_path, path)
			if rel_path:
				break
		else:
			logger.info("skip folder %s", msg_path)
			return

		if self.matches:
			name = os.path.basename(rel_path)
			logger.verbose("name %s, matching %d filters", name, len(self.matches))
			for obj in self.matches:
				if obj.match(name):
					break
			else:
				logger.info("filtered %s", rel_path)
				return

		path = os.path.join(self.live_root, rel_path)
		try:
			if not os.path.isdir(path):
				logger.verbose("not dir %s", path)
				return
		except OSError as e:
			logger.verbose("cannot access %s: %s", path)
			return

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
				logger.verbose("found media file %s", entry.name)
				self.add_media_file(entry.path)


	def service_actions(self):
		super().service_actions()
		if self.quit:
			logger.info("quitting")
			# server_close() runs when the with-block exits.
			# wait = False because we are interrupting serve_forever()
			# itself (waiting would deadlock).
			self.shutdown(wait = False)

		if self.clear:
			logger.info("clearing hash cache")
			self.clear = False
			if self.hash_cache is not None:
				self.hash_cache.clear()

		if self.reload:
			logger.info("reloading filter")
			self.reload = False
			self.reload_filter()


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
	logger.trace(args)

	logger.info("live-root %s, msg-addr %s, socket %s", args.path, args.msg_addr, args.socket)

	socket = args.socket and create_unix_socket(args.socket, mode = 0o660)

	with TranscriptionServer(transcription_handler, socket, args) as server:
		logger.debug("setup signal handlers")
		signal.signal(signal.SIGUSR1, server.on_signal)
		signal.signal(signal.SIGUSR2, server.on_signal)
		signal.signal(signal.SIGTERM, server.on_signal)
		signal.signal(signal.SIGINT, server.on_signal)
		server.serve_forever()

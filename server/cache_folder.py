#!/usr/bin/env python3

import os
import queue
import threading
import logging
from stat import S_ISREG
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

logger = logging.getLogger("cache_folder")


class cache_folder(FileSystemEventHandler):
	def __init__(self, root, max_size, del_func, bypass = set()):
		logger.debug("cache_folder on %s, max_size %d", root, max_size)
		self.root = root
		self.del_func = del_func
		self.max_size = max_size
		self.bypass = bypass.copy()
		self.reload()

		self.queue = queue.Queue(maxsize = 0x40)
		self.worker_thread = threading.Thread(target = self.worker_func, daemon = True)
		self.worker_thread.start()

		self.observer = Observer()
		self.observer.schedule(self, root, recursive = True)
		self.observer.start()

	def reload(self):
		cache = {}
		size = 0
		for path, dirs, files in os.walk(self.root):
			assert(path.startswith(self.root))
			rel_path = path[len(self.root):].lstrip("/.")
			for f in files:
				name = os.path.join(rel_path, f)
				if name in self.bypass:
					continue
				try:
					stat = os.lstat(os.path.join(path, f))
					cache[name] = stat
					size += stat.st_size
				except Exception:
					pass

		logger.debug("reload %s: %d/%d", self.root, size, self.max_size)
		self.cache, self.cur_size = cache, size

	def on_any_event(self, ev):
		# logger.debug("%s event %s: %s %s", ev.is_directory and "dir" or "file", ev.event_type, ev.src_path, (ev.event_type == "moved") and ev.dest_path or "")
		if (not ev.is_directory) and ev.event_type in ("created", "closed", "moved", "modified", "deleted"):
			name = ev.src_path
			assert(name.startswith(self.root))
			name = name[len(self.root):].lstrip("/.")
			self.queue.put(name)

			if ev.event_type == "moved":
				name = ev.dest_path
				assert(name.startswith(self.root))
				name = name[len(self.root):].lstrip("/.")
				self.queue.put(name)

	def worker_func(self):
		while True:
			path = None
			try:
				path = self.queue.get_nowait()
			except queue.Empty:
				logger.debug("%s: %d/%d", self.root, self.cur_size, self.max_size)
				if self.cur_size > self.max_size:
					del_list = sorted(self.cache.items(), key = (lambda obj: obj[1].st_mtime))
					for rec in del_list:
						if self.del_func(self.root, *rec):
							break
				path = self.queue.get()

			if path not in self.bypass:
				try:
					stat = os.lstat(os.path.join(self.root, path))
					if not S_ISREG(stat.st_mode):
						continue
					record = self.cache.get(path)
					if record:
						self.cur_size -= record.st_size

					self.cur_size += stat.st_size
					self.cache[path] = stat

				except Exception:
					record = self.cache.get(path)
					if record:
						self.cur_size -= record.st_size
						del self.cache[path]

			self.queue.task_done()


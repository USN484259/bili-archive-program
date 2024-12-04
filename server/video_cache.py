#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import time
import asyncio
import logging
import argparse
import multiprocessing
from collections import deque
from urllib.parse import parse_qs
from fcgi_server import FcgiServer, HttpResponseMixin
from fastcgi import FcgiHandler

from cache_folder import cache_folder

# constants

import constants

# static objects

logger = logging.getLogger("bili_arch.video_cache")
multiprocessing = multiprocessing.get_context("fork")

# helper functions

def exec_download(video_root, bvid, arg_list):
	import core
	import runtime
	import network
	import video

	async def download_main(args):
		async with network.session() as sess:
			await video.download(sess, bvid, video_root, mode = args.mode, prefer = args.prefer, reject = args.reject)

	args = runtime.parse_args(("auth", "network", "bandwidth", "video_mode", "prefer"), arg_list = arg_list)
	asyncio.run(download_main(args))


# classes

class BVDownloader:
	def __init__(self, video_root, /, args = (), *, max_queue = 0x10, max_records = 0x400):
		self.video_root = video_root
		self.args = args
		self.record = deque([], max_records)
		self.queue = deque([], max_queue)
		self.task = None

	def download(self, bvid):
		task = multiprocessing.Process(target = exec_download, args = (self.video_root, bvid, self.args), daemon = False)
		task.start()
		return task

	def update(self):
		if self.task is not None and not self.task.is_alive():
			exitcode = self.task.exitcode
			self.task.close()
			self.task = None
			rec = self.queue.popleft()
			logger.info("task end %s (%d)", rec["bvid"], exitcode)
			if rec["status"] != "cancelled":
				assert(rec["status"] == "running")
				if exitcode == 0:
					rec["status"] = "done"
				else:
					rec["status"] = "failed %d" % exitcode
			rec["stop_time"] = int(time.time() * 1000)
			self.record.append(rec)

		while self.task is None and self.queue:
			rec = self.queue[0]
			if rec["status"] == "cancelled":
				self.queue.popleft()
				continue
			assert(rec["status"] == "waiting")
			logger.info("task start %s", rec["bvid"])
			self.task = self.download(rec["bvid"])
			rec["start_time"] = int(time.time() * 1000)
			rec["status"] = "running"


	def schedule(self, bvid):
		if len(self.queue) >= self.queue.maxlen:
			return False

		self.queue.append({
			"bvid": bvid,
			"status": "waiting",
			"create_time": int(time.time() * 1000)
		})
		self.update()
		return True


	def cancel(self, bvid):
		for rec in self.queue:
			if rec["bvid"] == bvid:
				if self.task is not None and self.queue[0] is rec:
					logger.info("cancelling task %s", bvid)
					self.task.terminate()
				rec["status"] = "cancelled"
				self.update()
				return True
		return False


	def status(self, since):
		logger.debug("read status since %d", since)
		def make_diff_list(rec_list, since, check_first = False):
			if since == 0:
				return list(rec_list)

			result = []
			i = len(rec_list)
			while (i > 0):	# reverse iteration
				rec = rec_list[i-1]
				if rec.get("create_time", 0) <= since and rec.get("start_time", 0) <= since and rec.get("stop_time", 0) <= since:
					break
				result.append(rec)
				i -= 1

			if check_first and i > 0:
				rec = rec_list[0]
				if rec["status"] == "running":
					result.append(rec)

			return list(reversed(result))

		return {
			"timestamp": int(time.time() * 1000),
			"since": since,
			"queue": make_diff_list(self.queue, since, True),
			"record": make_diff_list(self.record, since),
		}


class bv_play_handler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			req_method = self.environ["REQUEST_METHOD"]
			if req_method not in ("GET", "POST", "HEAD"):
				return self.send_response(405)

			if req_method in ("GET", "HEAD"):
				query_str = self.environ.get("QUERY_STRING")
				since = 0
				if query_str:
					query = parse_qs(query_str, strict_parsing = True)
					try:
						since = int(query.get("since")[0])
					except (ValueError, IndexError):
						pass
				result = self.server.status(since)
				return self.send_response(200, "application/json", result)

			else:	# POST
				query_str = self["stdin"].read().decode("utf-8")
				logger.info("query: %s", query_str)
				query = parse_qs(query_str, strict_parsing = True)

				if not query:
					return self.send_response(400)
				elif "cancel" in query:
					bvid = constants.bv_pattern.fullmatch(query["cancel"][0])[1]
					result = self.server.cancel(bvid)
					if result:
						return self.send_response(200, "application/json", result)
					else:
						return self.send_response(404)
				elif "bvid" in query:
					bvid = constants.bv_pattern.fullmatch(query["bvid"][0])[1]
					result = self.server.schedule(bvid)
					if result:
						return self.send_response(200, "application/json", result)
					else:
						return self.send_response(429)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class BVPlayServer(FcgiServer):
	def __init__(self, handler, cache_root, cache_size, args):
		FcgiServer.__init__(self, handler)
		self.bv_cache = cache_folder(cache_root, cache_size, self.cache_del_func)
		self.bv_downloader = BVDownloader(cache_root, args)

	@staticmethod
	def cache_del_func(root, path, stat):
		name = os.path.join(root, path)
		logger.debug("removing %s", name)
		result = False
		try:
			os.remove(name)
			result = True
			os.removedirs(os.path.split(name)[0])
		except Exception:
			pass
		return result

	def schedule(self, bvid):
		result = self.bv_downloader.schedule(bvid)
		if result:
			return {
				"scheduled": bvid
			}

	def cancel(self, bvid):
		result = self.bv_downloader.cancel(bvid)
		if result:
			return {
				"cancelled": bvid
			}

	def status(self, since = None):
		return self.bv_downloader.status(since or 0)

	def service_actions(self):
		super().service_actions()
		self.bv_downloader.update()

# entrance

if __name__ == "__main__":
	logging.basicConfig(level = logging.INFO, format = constants.LOG_FORMAT, stream = sys.stderr)

	parser = argparse.ArgumentParser()
	parser.add_argument("--cache", required = True)
	parser.add_argument("--size", required = True)
	parser.add_argument("args", nargs = '*')

	args = parser.parse_args()
	cache_size = constants.number_with_unit(args.size)

	logger.info("cache at %s size %d", args.cache, cache_size)

	with BVPlayServer(bv_play_handler, args.cache, cache_size, args.args) as server:
		server.serve_forever(poll_interval = 2)

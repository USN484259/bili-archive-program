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

# constants

import constants

# static objects

logger = logging.getLogger("bili_arch.video_cache")
multiprocessing = multiprocessing.get_context("fork")

# helper functions

def exec_download(video_root, bvid, pipe, arg_list, extra_args):
	import core
	import runtime
	import network
	import video

	async def download_main(args):
		try:
			stall = runtime.Stall()
			async with network.session() as sess:
				await video.download(sess, bvid, video_root, args.mode, stall = stall, prefer = args.prefer, reject = args.reject, **extra_args)
		except Exception as e:
			pipe.send(str(e))
			raise

	args = runtime.parse_args(("auth", "network", "bandwidth", "video_mode", "prefer"), arg_list = arg_list)
	asyncio.run(download_main(args))


# classes

class BVDownloader:
	def __init__(self, video_root, /, args = (), *, max_queue = 0x10, max_records = 0x400):
		self.video_root = video_root
		self.args = args
		self.record = deque([], max_records)
		self.queue = deque([], max_queue)
		self.pipe = multiprocessing.Pipe(False)
		self.task = None
		self.msg = None

	def download(self, bvid, extra_args):
		self.msg = None
		task = multiprocessing.Process(target = exec_download, args = (self.video_root, bvid, self.pipe[1], self.args, extra_args), daemon = False)
		task.start()
		return task

	def update(self):
		try:
			if self.pipe[0].poll():
				self.msg = str(self.pipe[0].recv())
				logger.debug("got message %s", self.msg)
		except Exception:
			logger.exception("exception in polling pipe")

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
					rec["status"] = "failed %s" % (self.msg or "(unknown error)")
			rec["stop_time"] = int(time.time() * 1000)
			self.record.append(rec)

		while self.task is None and self.queue:
			rec = self.queue[0]
			if rec["status"] == "cancelled":
				self.queue.popleft()
				continue
			assert(rec["status"] == "waiting")
			logger.info("task start %s", rec["bvid"])
			self.task = self.download(rec["bvid"], rec.get("args", {}))
			rec["start_time"] = int(time.time() * 1000)
			rec["status"] = "running"


	def schedule(self, bvid, args = {}):
		if len(self.queue) >= self.queue.maxlen:
			return False

		self.queue.append({
			"bvid": bvid,
			"args": args,
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
		def make_diff_list(rec_list, since):
			if since == 0:
				return list(rec_list)

			result = []
			i = len(rec_list)
			while (i > 0):	# reverse iteration
				i -= 1
				rec = rec_list[i]
				if rec.get("create_time", 0) <= since and rec.get("start_time", 0) <= since and rec.get("stop_time", 0) <= since:
					break
				result.append(rec)

			return list(reversed(result))

		result = {
			"timestamp": int(time.time() * 1000),
			"since": since,
			"queue": make_diff_list(self.queue, since),
			"record": make_diff_list(self.record, since),
		}

		if self.queue and self.queue[0].get("status", "") == "running":
			result["running"] = self.queue[0]

		return result


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
				query = parse_qs(query_str, keep_blank_values = True, strict_parsing = True)

				if not query:
					return self.send_response(400)
				elif "bvid" in query:
					bv_match = constants.bvid_pattern.fullmatch(query["bvid"][0])
					if not bv_match:
						return self.send_response(400)
					bvid = bv_match[1]
					args = {}
					if self.server.max_duration:
						args["max_duration"] = self.server.max_duration
					max_duration_param = query.get("max_duration")
					if max_duration_param and max_duration_param[0]:
						try:
							logger.debug(max_duration_param)
							duration = int(max_duration_param[0])
							if duration > 0:
								args["max_duration"] = duration
							else:
								del args["max_duration"]

						except ValueError:
							return self.send_response(400)

					if "audio_only" in query:
						args["ignore"] = 'V'

					result = self.server.schedule(bvid, args)
					if result:
						return self.send_response(200, "application/json", result)
					else:
						return self.send_response(429)
				else:
					return self.send_response(400)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class BVPlayServer(FcgiServer):
	def __init__(self, handler, cache_root, cache_size, max_duration, args):
		FcgiServer.__init__(self, handler)
		if cache_size:
			from cache_folder import cache_folder
			self.bv_cache = cache_folder(cache_root, cache_size, self.cache_del_func)
		self.max_duration = max_duration
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

	def schedule(self, bvid, args):
		result = self.bv_downloader.schedule(bvid, args)
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
	parser.add_argument("--path", required = True)
	parser.add_argument("--max-size")
	parser.add_argument("--max-duration", type = int)
	parser.add_argument("args", nargs = '*')

	args = parser.parse_args()
	max_size = None
	if args.max_size:
		max_size = constants.number_with_unit(args.max_size)

	logger.info("cache at %s, max_size %s, max_duration %s", args.path, str(max_size), str(args.max_duration))
	cache_path = os.path.realpath(args.path)
	os.makedirs(cache_path, exist_ok = True)

	with BVPlayServer(bv_play_handler, args.path, max_size, args.max_duration, args.args) as server:
		server.serve_forever(poll_interval = 2)

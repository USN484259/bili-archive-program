#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import time
import signal
import asyncio
import logging
import argparse
import multiprocessing
from collections import deque
from urllib.parse import parse_qs
from contextlib import suppress
from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler

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
	def __init__(self, video_root, /, db_func = None, args = (), *, max_queue = 0x10, max_history = 0x10):
		self.video_root = video_root
		self.args = args
		self.history = deque([], max_history)
		self.queue = deque([], max_queue)
		self.pipe = multiprocessing.Pipe(False)
		self.db_func = db_func
		self.task = None
		self.msg = None

	def download(self, bvid, extra_args):
		self.msg = None
		task = multiprocessing.Process(target = exec_download, args = (self.video_root, bvid, self.pipe[1], self.args, extra_args), daemon = False)
		task.start()
		return task

	def update(self):
		done_bvid = None

		if not self.queue:
			return

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
			done_bvid = rec["bvid"]
			self.history.append(rec)

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

		if done_bvid and callable(self.db_func):
			self.db_func(done_bvid)


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
					assert(rec["status"] == "running")
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
				if rec.get("create_time", 0) < since and rec.get("start_time", 0) < since and rec.get("stop_time", 0) < since:
					break
				result.append(rec)

			return list(reversed(result))

		result = {
			"timestamp": int(time.time() * 1000),
			"since": since,
			"queue": make_diff_list(self.queue, since),
			"history": make_diff_list(self.history, since),
		}

		if self.queue and self.queue[0].get("status", "") == "running":
			result["running"] = self.queue[0].get("start_time", 0)

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
					with suppress(ValueError, KeyError, IndexError):
						query = parse_qs(query_str, strict_parsing = True)
						since = int(query["since"][0])

				result = self.server.status(since)
				return self.send_response(200, "application/json", result)

			else:	# POST
				try:
					query_str = self.read().decode("utf-8")
					logger.info("query: %s", query_str)

					query = parse_qs(query_str, keep_blank_values = True, strict_parsing = True)
					bv_match = constants.bvid_pattern.fullmatch(query["bvid"][0])
					bvid = bv_match[1]
				except (TypeError, ValueError, KeyError, IndexError, UnicodeError):
					return self.send_response(400)

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

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class BVPlayServer(FcgiServer):
	def __init__(self, handler, cache_root, db_path, max_duration, args):
		FcgiServer.__init__(self, handler)
		self.max_duration = max_duration
		if db_path:
			from video_database import VideoDatabaseManager
			self.database = VideoDatabaseManager(cache_root, db_path)
			self.need_walk = True
			signal.signal(signal.SIGUSR1, self.sig_walk)

		self.bv_downloader = BVDownloader(cache_root, self.handle_db_update, args)

	def sig_walk(self, signum, frame):
		logger.info("walk scheduled")
		self.need_walk = True

	def handle_db_update(self, bvid):
		if not self.database:
			return
		try:
			logger.info("updating db %s", bvid)
			if self.database.update_video(bvid):
				self.database.update_video_size(bvid)
		except Exception:
			logger.exception("failed in updating database for %s", bvid)

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
		if self.need_walk:
			logger.info("walking video cache")
			try:
				self.database.walk()
			finally:
				self.need_walk = False

# entrance

if __name__ == "__main__":
	logging.basicConfig(level = logging.INFO, format = constants.LOG_FORMAT, stream = sys.stderr)

	parser = argparse.ArgumentParser()
	parser.add_argument("--path", required = True)
	parser.add_argument("--database")
	parser.add_argument("--max-duration", type = int)
	parser.add_argument("args", nargs = '*')

	args = parser.parse_args()

	logger.info("cache at %s, database %s, max_duration %s", args.path, str(args.database), str(args.max_duration))
	cache_path = os.path.realpath(args.path)
	os.makedirs(cache_path, exist_ok = True)
	if args.database:
		db_path = os.path.split(args.database)[0]
		if db_path:
			db_path = os.path.realpath(db_path)
			os.makedirs(db_path, exist_ok = True)

	with BVPlayServer(bv_play_handler, args.path, args.database, args.max_duration, args.args) as server:
		server.serve_forever(poll_interval = 10)

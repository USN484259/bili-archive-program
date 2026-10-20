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

from utils import logger_init
from poller import Timer
from messaging import MessagingClient

# constants

import constants

POLL_INTERVAL_MS = 10 * 1000
MSG_RECONNECT_INTERVAL = 10

# static objects

logger = logging.getLogger("bili_arch.video_cache")
multiprocessing = multiprocessing.get_context("fork")

# helper functions

def exec_download(video_root, bvid, pipe, arg_list, extra_args):
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
			return False

		try:
			if self.pipe[0].poll():
				self.msg = str(self.pipe[0].recv())
				logger.verbose("got message from %d: %s", self.task.pid, self.msg)
		except Exception:
			logger.exception("exception in polling pipe")

		if self.task is not None and not self.task.is_alive():
			exitcode = self.task.exitcode
			logger.verbose("task %d exit %d", self.task.pid, exitcode)
			self.task.close()
			self.task = None
			rec = self.queue.popleft()

			if rec["status"] != "cancelled":
				assert(rec["status"] == "running")
				if exitcode == 0:
					rec["status"] = "done"
				else:
					rec["status"] = "failed %s" % (self.msg or "(unknown error)")
			stop_time = int(time.time() * 1000)
			rec["stop_time"] = stop_time
			done_bvid = rec["bvid"]
			logger.info("%s %s, duration %d", done_bvid, rec["status"], stop_time - rec["start_time"])
			self.history.append(rec)

		while self.task is None and self.queue:
			rec = self.queue[0]
			if rec["status"] == "cancelled":
				self.queue.popleft()
				continue
			assert(rec["status"] == "waiting")
			logger.info("%s start", rec["bvid"])
			self.task = self.download(rec["bvid"], rec.get("args", {}))
			logger.verbose("create task %d", self.task.pid)
			rec["start_time"] = int(time.time() * 1000)
			rec["status"] = "running"

		if done_bvid and callable(self.db_func):
			logger.debug("db callback for %s", done_bvid)
			self.db_func(done_bvid)

		return bool(self.queue)


	def schedule(self, bvid, args = {}):
		if len(self.queue) >= self.queue.maxlen:
			logger.notice("download queue full, refusing %s", bvid)
			return False

		self.queue.append({
			"bvid": bvid,
			"args": args,
			"status": "waiting",
			"create_time": int(time.time() * 1000)
		})
		logger.verbose("queued %s", bvid)
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
		logger.notice("cannot cancel %s", bvid)
		return False


	def status(self, since):
		logger.verbose("read status since %d", since)
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
		logger.trace(result)
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
				return self.send_response(200, json = result)

			else:	# POST
				try:
					query_str = self.read().decode("utf-8")
					logger.verbose("POST %s", query_str)

					query = parse_qs(query_str, keep_blank_values = True, strict_parsing = True)
					bv_match = constants.bvid_pattern.fullmatch(query["bvid"][0])
					bvid = bv_match[1]
				except (TypeError, ValueError, KeyError, IndexError, UnicodeError):
					logger.notice("bad POST %s", query_str)
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
						logger.notice("invalid max_duration")
						return self.send_response(400)

				if "audio_only" in query:
					args["ignore"] = 'V'

				logger.verbose("max_duration %s, ignore %s", args.get("max_duration"), args.get("ignore"))

				result = self.server.schedule(bvid, args)
				if result:
					logger.info("scheduled %s", bvid)
					return self.send_response(200, json = result)
				else:
					logger.info("cannot schedule %s", bvid)
					return self.send_response(429)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class BVPlayServer(FcgiServer):
	def __init__(self, handler, cache_root, /, db_path, msg_client, walk, max_duration, args):
		FcgiServer.__init__(self, handler)
		self.max_duration = max_duration
		self.bv_downloader = BVDownloader(cache_root, self.handle_db_update, args)
		self.database = None
		self.need_walk = False
		self.timer = Timer()
		self.msg_client = None

		if db_path:
			from video_database import VideoDatabaseManager
			self.database = VideoDatabaseManager(cache_root, db_path)
			self.need_walk = walk
			signal.signal(signal.SIGUSR1, self.sig_walk)

		self.add_watch(self.timer, lambda obj: obj.drain())
		if msg_client:
			if db_path:
				self.msg_client = msg_client
				self.add_watch(self.msg_client, self.handle_message)
			else:
				logger.notice("no database, closing msg_client")
				msg_client.close()


	def sig_walk(self, signum, frame):
		logger.info("signal walk")
		self.need_walk = True
		self.wakeup()


	def __enter__(self):
		res = super().__enter__()
		if self.database is not None:
			signal.signal(signal.SIGUSR1, self.sig_walk)
			if self.need_walk:
				self.wakeup()
		return res


	def handle_db_update(self, bvid):
		if not self.database:
			return
		try:
			logger.info("updating db %s", bvid)
			if self.database.update_video(bvid):
				self.database.update_video_size(bvid)
		except Exception:
			logger.exception("failed in updating database for %s", bvid)


	def handle_flags(self, info):
		path = info.get("path")
		if not (isinstance(path, str) and os.path.realpath(path) == self.database.get_root()):
			logger.verbose("ignore path %s", path)
			return

		action = info.get("action")
		if action != "flags":
			logger.verbose("path %s, ignore action %s", path, action)
			return

		bv_list = info.get("bv_list")
		add_flags = info.get("add", [])
		del_flags = info.get("del", [])

		is_str_list = lambda obj: isinstance(obj, list) and all(isinstance(item, str) for item in obj)

		if not (is_str_list(bv_list) and is_str_list(add_flags) and is_str_list(del_flags) and
			bv_list and (add_flags or del_flags)):
			logger.notice("invalid database message %s", str(info))
			return

		logger.verbose("updating flags, path %s", path)
		self.database.set_flags(bv_list, add_flags, del_flags)


	def handle_message(self, obj):
		assert(obj is self.msg_client)
		try:
			self.msg_client.wait(0)
			while True:
				topic, info = self.msg_client.recv()
				if not topic:
					break
				if not isinstance(info, dict):
					continue
				if topic != constants.topic.database:
					logger.warning("unexpected topic %s", topic)
					continue

				self.handle_flags(info)

		except Exception:
			logger.exception("exception in handle_message")


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
		if self.bv_downloader.update():
			self.timer.settime(POLL_INTERVAL_MS)

		if self.need_walk:
			logger.info("walking video cache")
			try:
				self.database.walk()
			finally:
				self.need_walk = False


# entrance

if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("--path", required = True)
	parser.add_argument("--database")
	parser.add_argument("--walk", action = "store_true")
	parser.add_argument("--msg-addr")
	parser.add_argument("--max-duration", type = int)
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("args", nargs = '*')

	args = parser.parse_args()
	logger_init(args.verbose)

	logger.info("cache at %s, database %s, max_duration %s, msg_addr %s", args.path, args.database, args.max_duration, args.msg_addr)
	cache_path = os.path.realpath(args.path)
	os.makedirs(cache_path, exist_ok = True)
	if args.database:
		db_path = os.path.split(args.database)[0]
		if db_path:
			db_path = os.path.realpath(db_path)
			os.makedirs(db_path, exist_ok = True)

	msg_client = None
	if args.msg_addr:
		msg_client = MessagingClient(args.msg_addr, reconnect = MSG_RECONNECT_INTERVAL)
		msg_client.subscribe(constants.topic.database)

	with BVPlayServer(bv_play_handler, args.path, db_path = args.database, msg_client = msg_client, walk = args.walk, max_duration = args.max_duration, args = args.args) as server:
		server.serve_forever()

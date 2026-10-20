#!/usr/bin/env python3

# This file is written with the assistance of opencode-deepseek-v4.1-flash

import os
import sys
sys.path[0] = os.getcwd()

import time
import json
import logging
from urllib.parse import urlparse, parse_qs
from collections import OrderedDict

from utils import logger_init
from messaging import MessagingClient
from video_database import VideoDatabase
from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler
from poller import Timer

# constants

import constants
QUERY_PAGE_LIMIT = 100
DB_CLOSE_TIMEOUT = 120
POLL_INTERVAL_MS = 60 * 1000
POST_MAX_LENGTH = 0x4000

# static objects

logger = logging.getLogger("bili_arch.video_query")


# classes

class QueryHandler(HttpResponseMixin, FcgiHandler):
	def handle_post(self):
		try:
			data = self.read(POST_MAX_LENGTH).decode()
			info = json.loads(data)
		except (UnicodeError, ValueError) as e:
			logger.notice("cannot handle POST: %s", e)
			return self.send_response(400)

		if not isinstance(info, dict):
			logger.notice("POST body is not a json object")
			return self.send_response(400)

		# POST forwards a request to the database daemon over the messaging
		# service; "path" is the doc-root-relative video root and the daemon
		# matches it against the paths it is watching. No database is opened here.
		path = info.get("path")
		action = info.get("action")
		if not (isinstance(path, str) and path):
			logger.notice("missing path in POST")
			return self.send_response(400)

		www_root = self.environ.get("DOCUMENT_ROOT")
		video_root = os.path.join(www_root, path.lstrip('/.'))
		logger.info("POST video_root %s, action %s", video_root, action)

		if action == "flags":
			bv_list = info.get("bv_list")
			add_flags = info.get("add") or []
			del_flags = info.get("del") or []
			is_str_list = lambda obj: isinstance(obj, list) and all(isinstance(item, str) for item in obj)
			if not (is_str_list(bv_list) and is_str_list(add_flags) and is_str_list(del_flags) and
				bv_list and (add_flags or del_flags)):
				logger.notice("invalid flags in POST")
				return self.send_response(400)

			# forward the request over the messaging bus; the daemon routes it by
			# video root and applies the flags through the database it owns
			args = {
				"path":		video_root,
				"action":	"flags",
				"bv_list":	bv_list,
				"add":		add_flags,
				"del":		del_flags,
			}
			result = self.server.set_flags(args)
			return self.send_response(result and 200 or 503)
		else:
			logger.notice("unknown action %s", action)
			return self.send_response(418)


	def handle_get(self):
		url = urlparse(self.environ.get("REQUEST_URI"))
		db_path = url.path.lstrip('/.')
		www_root = self.environ.get("DOCUMENT_ROOT")
		db = self.server.get(os.path.join(www_root, db_path))
		if not db:
			return self.send_response(404)

		query_str = self.environ.get("QUERY_STRING")
		try:
			query = parse_qs(query_str, strict_parsing = True)
			rules = dict(query.items())

			limit = int(rules.get("limit", (0, ))[0])
			limit = (limit > 0) and min(limit, QUERY_PAGE_LIMIT) or QUERY_PAGE_LIMIT
			rules["limit"] = (limit, )
		except (TypeError, ValueError, KeyError, IndexError):
			return self.send_response(400)
		try:
			result, count = db.query(rules)
			return self.send_response(200, json = {"count": count, "data": result})
		except Exception:
			return self.send_response(400)


	def handle(self):
		try:
			req_method = self.environ["REQUEST_METHOD"]

			if req_method in ("GET", "HEAD"):
				return self.handle_get()
			elif req_method == "POST":
				return self.handle_post()
			else:
				return self.send_response(405)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class VideoServer(FcgiServer):
	class Record:
		def __init__(self, db_path):
			logger.info("opening database %s", db_path)
			self.path = db_path
			self.db = VideoDatabase(db_path)
			self.atime = time.monotonic()


		def close(self):
			logger.info("closing database %s", self.path)
			self.db.close()


	def __init__(self, handler, msg_addr = None):
		FcgiServer.__init__(self, handler)
		self.db_map = OrderedDict()
		self.msg_client = None
		self.timer = Timer()
		self.msg_warned = False

		self.add_watch(self.timer, lambda obj: obj.drain())
		if msg_addr:
			self.msg_client = MessagingClient(msg_addr)


	def server_close(self):
		super().server_close()

		if self.msg_client is not None:
			self.msg_client.close()
			self.msg_client = None

		while self.db_map:
			db_path, rec = self.db_map.popitem(last = False)
			rec.close()


	def get(self, db_path):
		rec = self.db_map.get(db_path)
		if rec is not None:
			rec.atime = time.monotonic()
			self.db_map.move_to_end(db_path, last = True)
			return rec.db

		try:
			rec = self.Record(db_path)
			if not self.db_map:	# previously no open db, need to start timer
				logger.verbose("start timer")
				self.timer.settime(POLL_INTERVAL_MS, repeat = True)

			self.db_map[db_path] = rec
			self.db_map.move_to_end(db_path, last = True)
			return rec.db
		except Exception as e:
			logger.notice("cannot open database %s: %s", db_path, e)
			return None


	def service_actions(self):
		super().service_actions()
		cur_time = time.monotonic()
		while self.db_map:
			rec = next(iter(self.db_map.values()))
			if rec.atime + DB_CLOSE_TIMEOUT >= cur_time:
				return
			db_path, r = self.db_map.popitem(last = False)
			assert(r is rec)
			rec.close()

		# all db closed, cancel timer
		logger.verbose("stop timer")
		self.timer.cancel()


	def set_flags(self, args):
		if self.msg_client is None:
			if not self.msg_warned:
				logger.notice("no msg_addr, skip set_flags")
				self.msg_warned = True
			return False

		if not self.msg_client.send(constants.topic.database, json = args):
			if not self.msg_warned:
				logger.notice("cannot send set_flags")
				self.msg_warned = True
			return False

		self.msg_warned = False
		return True


# entrance

if __name__ == "__main__":
	import argparse

	parser = argparse.ArgumentParser()
	parser.add_argument("--msg-addr")
	parser.add_argument("-v", "--verbose", action = "count", default = 0)

	args = parser.parse_args()
	logger_init(args.verbose)
	logger.trace(args)

	with VideoServer(QueryHandler, args.msg_addr) as server:
		server.serve_forever()

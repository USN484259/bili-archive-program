#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import time
import logging
from urllib.parse import urlparse, parse_qs
from collections import OrderedDict


from video_database import VideoDatabase
from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler

# constants

import constants
QUERY_PAGE_LIMIT = 100
DB_CLOSE_TIMEOUT = 120

# static objects

logger = logging.getLogger("bili_arch.video_query")


# classes

class QueryHandler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			req_method = self.environ["REQUEST_METHOD"]
			if req_method not in ("GET", "HEAD"):
				return self.send_response(405)

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
				return self.send_response(200, "application/json", {"count": count, "data": result})
			except Exception:
				return self.send_response(400)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class VideoServer(FcgiServer):
	def __init__(self, handler):
		FcgiServer.__init__(self, handler)
		self.db_map = OrderedDict()

	def get(self, db_path):
		rec = self.db_map.get(db_path)
		if rec:
			rec["atime"] = int(time.time())
			self.db_map.move_to_end(db_path, last = True)
			return rec["database"]

		try:
			logger.info("opening database %s", db_path)
			db = VideoDatabase(db_path)
			rec = {
				"database": db,
				"atime": int(time.time())
			}
			self.db_map[db_path] = rec
			self.db_map.move_to_end(db_path, last = True)
			return db
		except Exception as e:
			logger.exception("")
			return None

	def service_actions(self):
		super().service_actions()
		cur_time = int(time.time())
		while self.db_map:
			rec = next(iter(self.db_map.values()))
			if rec["atime"] + DB_CLOSE_TIMEOUT >= cur_time:
				return
			db_path, r = self.db_map.popitem(last = False)
			assert(r is rec)
			db = rec["database"]
			logger.info("closing database %s", db_path)
			db.close()

# entrance

if __name__ == "__main__":
	logging.basicConfig(level = logging.DEBUG, format = constants.LOG_FORMAT, stream = sys.stderr)

	with VideoServer(QueryHandler) as server:
		server.serve_forever(poll_interval = 10)

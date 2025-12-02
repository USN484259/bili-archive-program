#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()
import logging
import argparse
from urllib.parse import parse_qs


from video_database import VideoDatabase
from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler

# constants

import constants
QUERY_PAGE_LIMIT = 100

# static objects

logger = logging.getLogger("bili_arch.video_query")


# classes

class QueryHandler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			req_method = self.environ["REQUEST_METHOD"]
			if req_method not in ("GET", "HEAD"):
				return self.send_response(405)

			query_str = self.environ.get("QUERY_STRING")
			try:
				query = parse_qs(query_str, strict_parsing = True)
				rules = { k : v[0] for k, v in query.items() }

				limit = int(rules.get("limit", 0))
				limit = (limit > 0) and min(limit, self.server.page_limit) or self.server.page_limit
				rules["limit"] = limit
			except (TypeError, ValueError, KeyError, IndexError):
				return self.send_response(400)

			result = self.server.query(rules)
			return self.send_response(200, "application/json", result)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class VideoServer(FcgiServer):
	def __init__(self, handler, db_path, page_limit = None):
		FcgiServer.__init__(self, handler)
		self.database = VideoDatabase(db_path)
		self.page_limit = (page_limit and page_limit > 0) and page_limit or QUERY_PAGE_LIMIT

	def query(self, rules):
		return self.database.query(rules)


# entrance

if __name__ == "__main__":
	logging.basicConfig(level = logging.INFO, format = constants.LOG_FORMAT, stream = sys.stderr)

	parser = argparse.ArgumentParser()
	parser.add_argument("--limit", type = int)
	parser.add_argument("database")

	args = parser.parse_args()
	with VideoServer(QueryHandler, args.database, args.limit) as server:
		server.serve_forever(poll_interval = 10)
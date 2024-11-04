#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import time
import httpx
import logging
from urllib.parse import unquote, parse_qs
from fcgi_server import FcgiServer, HttpResponseMixin
from fastcgi import FcgiHandler

# constants

import constants

# static objects

logger = logging.getLogger("bili_arch.bili_proxy")

url_whitelist = (
	(re.compile(r"^https://api[.]bilibili[.]com/x/web-interface/archive/related([?].+)?$"), 2),
	(re.compile(r"^http[s]?://[^/]+[.]hdslb[.]com/.+$"), 0),
)



# classes

class bili_proxy_handler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			sess = self.server.sess
			method = self.environ["REQUEST_METHOD"]
			if method not in ("GET", "POST", "HEAD"):
				return self.send_response(405)

			logger.debug(self.environ["QUERY_STRING"])
			query_str = unquote(self.environ["QUERY_STRING"])
			logger.debug(query_str)
			query = parse_qs(query_str)
			logger.debug(query)
			url = query["target"][0]

			logger.debug("%s %s", method, url)
			next_stall_time = None
			for regex, stall_time in url_whitelist:
				if regex.match(url):
					next_stall_time = stall_time
					logger.debug("target stall time %d", stall_time)
					break
			else:
				logger.warning("forbidden target %s", url)
				return self.send_response(403)

			try:
				if next_stall_time > 0:
					self.server.stall(next_stall_time)

				if method == "POST":
					content_type = self.environ.get("CONTENT_TYPE", "application/json")
					data = self["stdin"].read()

					resp = sess.request(method, url, headers = {'Content-Type': content_type}, data = data)
				else:
					resp = sess.request(method, url)

			except httpx.TimeoutException:
				return self.send_response(504)

			content_type = resp.headers.get("content-type", "application/json")
			headers = []
			for item in resp.headers.items():
				if item[0] in ("content-length", "content-range"):
					headers.append("%s: %s" % item)

			return self.send_response(resp.status_code, mime_type = content_type, extra_headers = headers, data = resp.read())

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class BiliProxyServer(FcgiServer):
	def __init__(self, handler, *, timeout = 30, cookies = {}):
		super().__init__(handler)
		self.sess = httpx.Client(headers = constants.USER_AGENT, timeout = timeout, cookies = cookies, follow_redirects = True)
		self.stall_time = 0
		self.last_time = 0


	def stall(self, next_stall_time):
		cur_time = time.monotonic()
		time_diff = cur_time - self.last_time
		time_wait = self.stall_time - time_diff
		logger.debug("stall %.1f sec",  max(time_wait, 0))
		if time_diff > 0 and time_wait > 0:
			time.sleep(time_wait)
			cur_time = time.monotonic()
		self.last_time = cur_time
		self.stall_time = next_stall_time


# entrance

if __name__ == "__main__":
	logging.basicConfig(level = logging.DEBUG, format = constants.LOG_FORMAT, stream = sys.stderr)

	with BiliProxyServer(bili_proxy_handler) as server:
		server.serve_forever(poll_interval = 5)

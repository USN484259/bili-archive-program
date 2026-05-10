#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import time
import httpx
import asyncio
import logging
from collections import namedtuple
from urllib.parse import urlparse, unquote, parse_qs
from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler

import constants
import runtime
import network


# static objects

logger = logging.getLogger("bili_arch.bili_proxy")

class whitelist_item:
	def __init__(self, regex, /, stall_time = 5, need_sign = False):
		self.regex = re.compile(regex)
		self.need_sign = need_sign
		self.stall_time = stall_time
		self.stall_until = 0

	def match(self, url):
		return bool(self.regex.fullmatch(url))

	def check_stall(self, cur_time):
		if cur_time < self.stall_until:
			return False
		self.stall_until = cur_time + self.stall_time
		return True


url_whitelist = (
	whitelist_item(r"^https://api[.]bilibili[.]com/x/web-interface/archive/related([?].+)?$", 5),
	whitelist_item(r"^https://api[.]bilibili[.]com/x/polymer/web-dynamic/v1/feed/space([?].+)?$", 5, need_sign = True),
)


# classes

class bili_proxy_handler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			method = self.environ["REQUEST_METHOD"]
			if method not in ("GET", "POST", "HEAD"):
				return self.send_response(405)

			url = unquote(self.environ["QUERY_STRING"])
			logger.debug(url)
			wbi_sign = None

			logger.debug("%s %s", method, url)
			cur_time = time.monotonic()
			for item in url_whitelist:
				if not item.match(url):
					continue
				if not item.check_stall(cur_time):
					return send_response(429)
				wbi_sign = item.need_sign
				break
			else:
				logger.warning("forbidden target %s", url)
				return self.send_response(403)

			try:
				if method == "POST":
					content_type = self.environ.get("CONTENT_TYPE", "")
					data = self["stdin"].read()
					coroutine = network.request(self.server.sess, method, url, headers = {'Content-Type': content_type}, data = data)

				else:
					url_split = url.partition('?')
					params = parse_qs(url_split[2], strict_parsing = True)
					for key in params.keys():
						params[key] = params[key][0]

					coroutine = network.request(self.server.sess, method, url_split[0], wbi_sign = wbi_sign, params = params)

				resp = self.server.event_loop.run_until_complete(coroutine)

			except httpx.TimeoutException:
				return self.send_response(504)

			except network.BiliApiError as e:
				return self.send_response(403, data = str(e))

			return self.send_response(200, mime_type = "application/json", data = resp)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)


class BiliProxyServer(FcgiServer):
	def __init__(self, handler):
		super().__init__(handler)
		self.event_loop = asyncio.new_event_loop()
		asyncio.set_event_loop(self.event_loop)
		self.sess = network.session()


# entrance

if __name__ == "__main__":
	args = runtime.parse_args(("network", "auth"), opt_auth = True)

	with BiliProxyServer(bili_proxy_handler) as server:
		server.serve_forever(poll_interval = 600)

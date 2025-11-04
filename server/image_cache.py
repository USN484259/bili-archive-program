#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import httpx
import logging
import argparse
from urllib.parse import urlparse, unquote, parse_qs
from contextlib import suppress
from simple_fastcgi import FcgiServer, HttpResponseMixin, FcgiHandler


# constants

import constants

# static objects

logger = logging.getLogger("bili_arch.image_cache")
image_url_pattern = re.compile(r"^http[s]?://[^/]+[.]hdslb[.]com/.+$")


# server & handler

class ImageCacheServer(FcgiServer):
	def __init__(self, handler, cache_root, /, cache_size = None, timeout = 30):
		FcgiServer.__init__(self, handler)
		self.cache_root = cache_root
		self.sess = httpx.Client(headers = constants.USER_AGENT, timeout = timeout, follow_redirects = True)
		if cache_size:
			from cache_folder import cache_folder
			self.image_cache = cache_folder(cache_root, cache_size, self.cache_del_func)

	@staticmethod
	def cache_del_func(root, path, stat):
		name = os.path.join(root, path)
		logger.debug("removing %s", name)
		with suppress(OSError):
			os.remove(name)
		return True

	def get(self, url):
		url_info = urlparse(url)
		filename = os.path.split(url_info.path)[1]
		cache_file = os.path.join(self.cache_root, filename)
		has_file = os.access(cache_file, os.F_OK)
		if not has_file:
			try:
				self.fetch(url, cache_file)
				has_file = os.access(cache_file, os.F_OK)
			except Exception as e:
				logger.error(str(e))

		return has_file and cache_file or None

	def fetch(self, url, path):
		with self.sess.stream("GET", url) as resp:
			resp.raise_for_status()
			with open(path, "xb") as f:
				for chunk in resp.iter_bytes():
					f.write(chunk)


class image_cache_handler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			doc_root = self.environ["DOCUMENT_ROOT"]
			req_method = self.environ["REQUEST_METHOD"]
			if req_method not in ("GET", "HEAD"):
				return self.send_response(405)

			url = unquote(self.environ["QUERY_STRING"])
			logger.debug(url)
			if not image_url_pattern.fullmatch(url):
				return self.send_response(403)

			cache_file = self.server.get(url)
			logger.debug(cache_file)
			if not cache_file:
				return self.send_response(404)

			rel_path = self.get_relative_path(cache_file, doc_root)
			logger.debug(rel_path)
			if not rel_path:
				return self.send_response(404)

			return self.send_redirect('/' + rel_path)

		except Exception:
			logger.exception("error in handle request")
			return self.send_response(500)

	@staticmethod
	def get_relative_path(path, doc_root):
		common_path = os.path.commonpath((path, doc_root))
		logger.debug("%s\t%s", common_path, doc_root)
		if common_path != doc_root:
			return None

		rel_path = os.path.relpath(path, doc_root)
		if ".." in rel_path:
			return None

		return rel_path

# entrance

if __name__ == "__main__":
	logging.basicConfig(level = logging.DEBUG, format = constants.LOG_FORMAT, stream = sys.stderr)

	parser = argparse.ArgumentParser()
	parser.add_argument("--path", required = True)
	parser.add_argument("--max-size")

	args = parser.parse_args()
	max_size = None
	if args.max_size:
		max_size = constants.number_with_unit(args.max_size)

	logger.info("image_cache at %s, max_size %s", args.path, str(max_size))
	image_root = os.path.realpath(args.path)
	os.makedirs(image_root, exist_ok = True)

	with ImageCacheServer(image_cache_handler, args.path, max_size) as server:
		server.serve_forever(poll_interval = 600)

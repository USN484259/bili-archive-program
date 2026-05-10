#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import httpx
import asyncio
import logging
import argparse
from collections import OrderedDict
from urllib.parse import urlparse, unquote, parse_qs
from contextlib import suppress
from simple_fastcgi import AsyncFcgiServer, AsyncHttpResponseMixin, AsyncFcgiHandler


# constants

import constants

# static objects

logger = logging.getLogger("bili_arch.image_cache")
image_url_pattern = re.compile(r"^http[s]?://[^/]+[.]hdslb[.]com/.+$")


# server & handler

class ImageCacheServer(AsyncFcgiServer):
	def __init__(self, handler, cache_root, /, cache_size = None, timeout = 30):
		super().__init__(handler)
		self.cache_root = cache_root
		self.cache_size = cache_size
		self.used_size = 0
		self.cache_table = OrderedDict()
		self.sess = httpx.AsyncClient(headers = constants.USER_AGENT, timeout = timeout, follow_redirects = True)
		if cache_size:
			self.scan_cache()

	def scan_cache(self):
		result = []
		with os.scandir(self.cache_root) as it:
			for entry in it:
				with suppress(Exception):
					if not entry.is_file():
						continue
					stat = entry.stat()
					result.append((
						entry.name,
						stat.st_size,
						int(stat.st_mtime * 1000)
					))
		result.sort(key = lambda e: e[2])
		for entry in result:
			self.cache_table[entry[0]] = entry[1]

	async def fetch(self, url, path):
		async with self.sess.stream("GET", url) as resp:
			resp.raise_for_status()
			size = 0
			with open(path, "xb") as f:
				async for chunk in resp.aiter_bytes():
					f.write(chunk)
					size += len(chunk)
			return size

	def drop_unused(self):
		while self.cache_table and self.used_size > self.cache_size:
			filename, size = self.cache_table.popitem(last = False)
			if asyncio.isfuture(size):
				logger.warning("got task when removing %s", filename)
				continue
			assert(isinstance(size, int))
			cache_file = os.path.join(self.cache_root, filename)
			logger.debug("removing %s", name)
			with suppress(OSError):
				os.remove(name)
			self.used_size -= size

	async def get(self, url):
		url_info = urlparse(url)
		filename = os.path.split(url_info.path)[1]
		record = self.cache_table.get(filename)
		if record is not None and asyncio.isfuture(record):
			with suppress(Exception):
				await record

		cache_file = os.path.join(self.cache_root, filename)
		has_file = os.access(cache_file, os.F_OK)

		if not has_file:
			try:
				task = asyncio.create_task(self.fetch(url, cache_file))
				self.cache_table[filename] = task
				size = await task
				self.cache_table[filename] = size
				self.used_size += size
				if self.cache_size:
					self.drop_unused()
				has_file = os.access(cache_file, os.F_OK)

			except Exception as e:
				logger.error(str(e))

		if has_file:
			with suppress(KeyError):
				self.cache_table.move_to_end(filename, last = True)
			return cache_file
		else:
			del self.cache_table[filename]


class image_cache_handler(AsyncHttpResponseMixin, AsyncFcgiHandler):
	async def handle(self):
		try:
			doc_root = self.environ["DOCUMENT_ROOT"]
			req_method = self.environ["REQUEST_METHOD"]
			if req_method not in ("GET", "HEAD"):
				return await self.send_response(405)

			url = unquote(self.environ["QUERY_STRING"])
			logger.debug(url)
			if not image_url_pattern.fullmatch(url):
				return await self.send_response(403)

			cache_file = await self.server.get(url)
			logger.debug(cache_file)
			if not cache_file:
				return await self.send_response(404)

			rel_path = self.get_relative_path(cache_file, doc_root)
			logger.debug(rel_path)
			if not rel_path:
				return await self.send_response(404)

			return await self.send_redirect('/' + rel_path)

		except Exception:
			logger.exception("error in handle request")
			return await self.send_response(500)

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

async def main(args):
	max_size = None
	if args.max_size:
		max_size = constants.number_with_unit(args.max_size)

	logger.info("image_cache at %s, max_size %s", args.path, str(max_size))
	image_root = os.path.realpath(args.path)
	os.makedirs(image_root, exist_ok = True)

	async with ImageCacheServer(image_cache_handler, args.path, max_size) as server:
		await server.serve_forever()


if __name__ == "__main__":
	logging.basicConfig(level = logging.DEBUG, format = constants.LOG_FORMAT, stream = sys.stderr)

	parser = argparse.ArgumentParser()
	parser.add_argument("--path", required = True)
	parser.add_argument("--max-size")

	args = parser.parse_args()
	asyncio.run(main(args))
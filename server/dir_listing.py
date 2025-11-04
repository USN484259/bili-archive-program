#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import json
import asyncio
from functools import partial
from urllib.parse import parse_qs

from simple_fastcgi import AsyncFcgiServer, AsyncHttpResponseMixin, AsyncFcgiHandler


async def dir_listing(path, ndjson = False):
	with os.scandir(path) as it:
		prepend = ""
		if not ndjson:
			yield "[\n"
		for entry in it:
			record = {
				"name": entry.name
			}
			try:
				if entry.is_dir():
					record["type"] = "dir"
				elif entry.is_file():
					record["type"] = "file"
				else:
					continue

				stat = entry.stat()
				record["size"] = stat.st_size
				record["mtime"] = int(stat.st_mtime * 1000)
			except Exception:
				pass

			line = prepend + json.dumps(record, ensure_ascii = False) + '\n'
			yield line

			if not ndjson:
				prepend = ",\n"

		if not ndjson:
			yield "]\n"


class dir_listing_handler(AsyncHttpResponseMixin, AsyncFcgiHandler):
	async def handle(self):
		try:
			doc_root = self.environ["DOCUMENT_ROOT"]
			accept = self.environ.get("HTTP_ACCEPT")
			query = parse_qs(self.environ["QUERY_STRING"])
			path = query["path"][0].lstrip("/.")
			is_ndjson = (accept and "ndjson" in accept)

			func = partial(dir_listing, os.path.join(doc_root, path), is_ndjson)
			return await self.send_response(200, is_ndjson and "application/x-ndjson" or "application/json", data = func)
		except Exception as e:
			return await self.send_response(404)


async def main():
	async with AsyncFcgiServer(dir_listing_handler) as server:
		await server.serve_forever()


if __name__ == "__main__":
	asyncio.run(main())
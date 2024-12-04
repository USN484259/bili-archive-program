#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

from urllib.parse import parse_qs
from fcgi_server import FcgiThreadingServer, HttpResponseMixin
from fastcgi import FcgiHandler


def dir_listing(path):
	result = []
	with os.scandir(path) as it:
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
			result.append(record)

	return result

class dir_listing_handler(HttpResponseMixin, FcgiHandler):
	def handle(self):
		try:
			doc_root = self.environ["DOCUMENT_ROOT"]
			query = parse_qs(self.environ["QUERY_STRING"])
			path = query["path"][0].lstrip("/.")

			data = dir_listing(os.path.join(doc_root, path))

			return self.send_response(200, "application/json", data)
		except Exception:
			return self.send_response(404)


if __name__ == "__main__":
	with FcgiThreadingServer(dir_listing_handler) as server:
		server.serve_forever()

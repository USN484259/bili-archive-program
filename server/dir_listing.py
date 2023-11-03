#!/usr/bin/env python3

import os
import json
from urllib.parse import parse_qs
from fcgi_server import FcgiServer
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

				stat = entry.stat()

				record["size"] = stat.st_size
				record["mtime"] = int(stat.st_mtime * 1000)
			except:
				pass
			result.append(record)

	return result

class dir_listing_handler(FcgiHandler):
	def handle(self):
		try:
			doc_root = self.environ["DOCUMENT_ROOT"]
			query = parse_qs(self.environ["QUERY_STRING"])
			path = query["path"][0].lstrip("/.")

			data = dir_listing(os.path.join(doc_root, path))

			self["stdout"].write(b"Content-type: text/json\r\nStatus: 200 OK\r\n\r\n")
			self["stdout"].write(bytes(json.dumps(data, indent = '\t', ensure_ascii = False), "utf-8"))
		except:
			self["stdout"].write(b"Content-type: text/plain\r\nStatus: 404 Not Found\r\n\r\n404 Not Found\r\n")

if __name__ == "__main__":
	with FcgiServer(dir_listing_handler) as server:
		server.serve_forever()

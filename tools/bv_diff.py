#!/usr/bin/env python3

import os
import sys
import json

set_a = set()
set_b = set()
with open(sys.argv[1], "r") as file_a:
	for line in file_a:
		bvid = line.rstrip()
		set_a.add(bvid)

with open(sys.argv[2], "r") as file_b:
	for line in file_b:
		bvid = line.rstrip()
		set_b.add(bvid)

print("loaded", file = sys.stderr)
bv_diff = set_a - set_b

removed = set_a - bv_diff

print("removed" + "\n".join(removed))

if False:

	result = []
	for bvid in bv_diff:
		try:
			with open(os.path.join(bvid, "info.json"), "r") as f:
				info = json.load(f)
			title = info["title"]
			author = info["owner"]["name"]
			result.append( (bvid, author, title) )
		except Exception:
			result.append( (bvid, "", "") )
		finally:
			print(bvid, file = sys.stderr)


	result.sort(key = lambda o: o[1])

	for obj in result:
		print(*obj, sep = '\t')



#!/usr/bin/env python3

import os
import sys
import json
import logging

import util

logger = logging.getLogger("bili_arch.build_index")


def build_video_index(path):
	bv_list = util.list_bv(path)
	logger.info("found %d video",  len(bv_list))
	index_table = []
	for bv in bv_list:
		logger.debug(bv)
		with open(os.path.join(path, bv, "info.json"), "r") as f:
			raw_info = json.load(f)
			info = {
				"bvid": raw_info.get("bvid"),
				"title": raw_info.get("title"),
				"zone": raw_info.get("tname"),
				"ctime": raw_info.get("ctime"),
				"duration": raw_info.get("duration"),
				"desc": raw_info.get("desc"),
				"pages": raw_info.get("videos")
			}
			if "staff" in raw_info:
				author_list = []
				for owner in raw_info.get("staff", []):
					author_list.append(owner.get("name"))
				info["author"] = author_list
			else:
				info["author"] = [raw_info.get("owner", {}).get("name")]
			index_table.append(info)

	return index_table


def main(args):
	out = sys.stdout
	logger.info("output to " + (args.out or "stdout"))
	if args.out:
		out = open(args.out, "w")

	video_root = args.dir or util.subdir("video")
	index_table = build_video_index(video_root)
	json.dump(index_table, out, indent = '\t', ensure_ascii = False)
	out.close()

if __name__ == "__main__":
	args = util.parse_args([
		(("-d", "--dir"), {}),
		(("-o", "--out"), {})
	])

	main(args)


#!/usr/bin/env python3

import os
import sys
import shutil
import logging
from contextlib import suppress

import runtime
import verify

from constants import topic
from utils import list_bv
from fops import locked_path, locked_file, staged_file
from messaging import MessagingClient

logger = logging.getLogger("bili_arch.merge_video")



def merge_part(src_part_path, dst_part_path, src_info, dst_info):
	def find_best(file_list):
		best = (0, None)
		for filename in file_list:
			score = 0
			name = os.path.basename(filename)
			with suppress(ValueError):
				score = int(name)
			if score > best[0]:
				best = (score, filename)
		return best

	for key in ('V', 'A'):
		src_score, src_best = find_best(src_info.get(key))
		dst_score, dst_best = find_best(dst_info.get(key))
		if src_score > dst_score:
			logger.verbose("copy %s", src_best)
			shutil.copy2(os.path.join(src_part_path, src_best), dst_part_path)
		else:
			logger.verbose("skip %s", src_best)

	# danmaku
	src_danmaku = src_info.get('D', [])
	dst_danmaku = dst_info.get('D', [])
	for filename in src_danmaku:
		if filename in dst_danmaku:
			logger.notice("merging danmaku not implemented")
		else:
			logger.verbose("copy danmaku")
			shutil.copy2(os.path.join(src_part_path, filename), dst_part_path)


def merge_video(src_path, dst_path, bvid):
	with locked_path(src_path, shared = True) as src_path, locked_path(dst_path) as dst_path:
		dst_info = verify.verify_bv(dst_path)
		if not verify.check_result(dst_info):
			logger.notice("dst: incomplete %s", bvid)
			logger.verbose(dst_info)

		src_info = verify.verify_bv(src_path)
		if not verify.check_result(src_info):
			logger.notice("src: incomplete %s", bvid)
			logger.verbose(src_info)

		# info
		if src_info["info"]:
			src_info_path = os.path.join(src_path, "info.json")
			dst_info_path = os.path.join(dst_path, "info.json")
			src_stat = os.stat(src_info_path)
			mode = '>'
			if dst_info["info"]:
				dst_stat = os.stat(dst_info_path)
				mode = (src_stat.st_mtime_ns <= dst_stat.st_mtime_ns) and '<' or '>'
			with locked_file(src_info_path, mode = "rb") as src, staged_file(dst_info_path, mode = "wb", rotate = mode) as dst:
				shutil.copyfileobj(src, dst)
				os.utime(dst, ns = (src_stat.st_atime_ns, src_stat.st_mtime_ns))

		for pfx in (default_names.rotate_postfix, default_names.backup_postfix):
			if os.path.isfile(src_info_path + pfx):
				logger.notice("merging %s not implemented", pfx)

		# cover
		src_cover = src_info.get("cover")
		if src_cover and src_cover != dst_info.get("cover"):
			src_cover_path = os.path.join(src_path, src_cover)
			dst_cover_path = os.path.join(dst_path, src_cover)
			if os.path.isfile(dst_cover_path):
				logger.verbose("skip cover %s", src_cover)
			else:
				logger.verbose("copy cover %s", src_cover)
				shutil.copy2(src_cover_path, dst_cover_path)

		# parts
		for cid, part_info in src_info["parts"].items():
			src_part_path = os.path.join(src_path, cid)
			dst_part_path = os.path.join(dst_path, cid)
			if os.path.isdir(dst_part_path):
				logger.verbose("merge part %s", cid)
				merge_part(src_part_path, dst_part_path, part_info, dst_info["parts"].get(cid))
			else:
				logger.verbose("copy part %s", cid)
				shutil.copytree(src_part_path, dst_part_path)


def main(args):
	src_root = args.src
	dst_root = args.dir or runtime.subdir("video")
	bv_list = []
	if args.inputs:
		bv_list = args.inputs
	else:
		bv_list = list_bv(src_root)

	msg_client = MessagingClient(args.msg_addr)

	logger.info("merging %d videos from %s to %s", len(bv_list), src_root, dst_root)
	for bvid in bv_list:
		src_path = os.path.join(src_root, bvid)
		dst_path = os.path.join(dst_root, bvid)
		done = False
		try:
			if not os.path.isdir(src_path):
				logger.notice("skipping %s", bvid)
				continue

			if os.path.isdir(dst_path):
				logger.info("merging video %s, bvid")
				merge_video(src_path, dst_path, bvid)
			else:
				logger.info("copy video %s", bvid)
				shutil.copytree(src_path, dst_path)

			done = True
		except Exception:
			logger.exception("exception in %s", bvid)

		finally:
			msg_client.send(topic.video, json = {
				"timestamp":	int(time.time()),
				"bvid":		bvid,
				"mode":		"merge",
				"path":		src_root,
				"done":		done,
			})


if __name__ == "__main__":
	args = runtime.parse_args(("dir", "messaging"), [
		(("-s", "--src"), {"required": True}),
		(("inputs",), {"nargs": '*'}),
	])

	main(args)

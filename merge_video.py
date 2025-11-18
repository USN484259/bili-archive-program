#!/usr/bin/env python3

import os
import sys
import shutil
import logging

import core
import runtime
import verify


logger = logging.getLogger("bili_arch.merge_video")


def merge_video(src_path, dst_path):
	with os.scandir(src_path) as it:
		for entry in it:
			try:
				dst_name = os.path.join(dst_path, entry.name)
				if entry.is_dir():
					if not os.path.isdir(dst_name):
						logger.info("copy dir %s", entry.name)
						shutil.copytree(entry.path, dst_name)
					else:
						logger.debug("skip dir %s", entry.name)
				elif entry.is_file():
					if not os.path.isfile(dst_name):
						logger.info("copy file %s", entry.name)
						shutil.copy2(entry.path, dst_name)
					else:
						logger.debug("skip file %s", entry.name)
				else:
					stat = entry.stat(follow_symlinks = False)
					logger.warning("ignoring node %s type %x", entry.name, stat.st_mode)

			except Exception:
				logger.exception("exception in %s", entry.path)


def main(args):
	src_root = args.src or "."
	dst_root = args.dir or runtime.subdir("video")
	bv_list = []
	if args.inputs:
		bv_list = args.inputs
	else:
		bv_list = runtime.list_bv(src_root)

	logger.info("merging %d videos from %s to %s", len(bv_list), src_root, dst_root)
	for bvid in bv_list:
		src_path = os.path.join(src_root, bvid)
		dst_path = os.path.join(dst_root, bvid)
		try:
			if not os.path.isdir(src_path):
				logger.warning("skipping %s", bvid)
				continue

			if os.path.isdir(dst_path):
				logger.info("checking video %s", bvid)
				result = verify.verify_bv(dst_path)
				if not verify.check_result(result):
					logger.warning("incomplete %s", bvid)
					logger.warning(result)
				merge_video(src_path, dst_path)
			else:
				logger.info("copy video %s", bvid)
				shutil.copytree(src_path, dst_path)

		except Exception:
			logger.exception("exception in %s", bvid)


if __name__ == "__main__":
	args = runtime.parse_args(("dir",), [
		(("-s", "--src"), {}),
		(("inputs",), {"nargs": '*'}),
	])

	main(args)

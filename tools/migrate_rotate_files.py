#!/usr/bin/env python3

import os
import sys
sys.path[0] = os.getcwd()

import re
import logging
import argparse
from contextlib import ExitStack
from zipfile import ZipFile, ZipInfo

from constants import default_names
from utils import logger_init, list_bv
from fops import locked_path, locked_file

logger = logging.getLogger("bili_arch.migrate_rotate_files")
old_zip_pattern = re.compile(r"^(.+?)-rotate[.]zip$")
old_file_pattern = re.compile(r"^(.+?)-(\d+)$")


def migrate(path, filename):
	zip_match = old_zip_pattern.fullmatch(filename)
	if not zip_match:
		return
	dst_name = zip_match.group(1) + default_names.rotate_postfix
	logger.info("%s: %s => %s", path, filename, dst_name)
	src_path = os.path.join(path, filename)
	dst_path = os.path.join(path, dst_name)
	with ExitStack() as stack:
		src_fobj = stack.enter_context(locked_file(src_path, "rb"))
		src_zip = stack.enter_context(ZipFile(src_fobj, mode = "r"))

		try:
			dst_fobj = stack.enter_context(locked_file(dst_path, "xb"))
		except FileExistsError:
			logger.warning("file exists, skipped: %s", dst_path)
			return

		dst_zip = stack.enter_context(ZipFile(dst_fobj, mode = "w"))

		for src_info in src_zip.infolist():
			old_name = src_info.filename
			file_match = old_file_pattern.fullmatch(old_name)
			if file_match:
				new_name = "%s.%s" % file_match[1:]
				logger.info("%s => %s", old_name, new_name)
				src_info.filename = new_name
			else:
				new_name = old_name
				logger.info("copy %s", old_name)

			dst_info = ZipInfo(new_name, src_info.date_time)
			with src_zip.open(src_info, "r") as src, dst_zip.open(dst_info, "w") as dst:
				shutil.copyfileobj(src, dst)

	return True


def main(args):
	video_root = args.path
	bv_list = list_bv(video_root)
	logger.info("migrating %s, %d videos", video_root, len(bv_list))
	for bvid in bv_list:
		with locked_path(video_root, bvid) as path:
			del_list = []
			with os.scandir(path) as it:
				for entry in it:
					if entry.is_file():
						res = migrate(path, entry.name)
						if res and args.delete:
							del_list.append(entry.name)

			for filename in del_list:
				logger.info("deleting %s", filename)
				try:
					del_path = os.path.join(path, filename)
					os.unlink(del_path)
				except Exception as e:
					logger.error("cannot delete %s: %s", del_path, e)


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("--delete", action = "store_true")
	parser.add_argument("path")
	args = parser.parse_args()

	logger_init(args.verbose)
	main(args)

#!/usr/bin/env python3

import os
import sys
import re
import json
import filecmp

bvid_pattern = re.compile(r"(BV1[1-9A-HJ-NP-Za-km-z]{9})")
vid_pattern = re.compile(r"(\d+)[.]m4v")


def check_better_video(src_path, dst_path, filename):
	cur_res = int(vid_pattern.fullmatch(filename)[1])
	best_video = 0
	with os.scandir(dst_path) as it:
		for entry in it:
			if entry.is_file():
				match = vid_pattern.fullmatch(entry.name)
				if match:
					res = int(match.group(1))
					best_video = max(best_video, res)

	if best_video > cur_res:
		print("found better video %d than %s" % (best_video, filename), file = sys.stderr)
		return True



def check_part(src_path, dst_path, prefix):
	result = []
	with os.scandir(src_path) as it:
		for entry in it:
			if entry.name[0] == '.':
				continue
			if entry.is_file():
				try:
					if filecmp.cmp(os.path.join(src_path, entry.name), os.path.join(dst_path, entry.name), shallow = True):
						continue
				except Exception as e:
					if entry.name.endswith(".m4v") and check_better_video(src_path, dst_path, entry.name):
						continue
					print(e, file = sys.stderr)

			result.append(os.path.join(prefix, entry.name))
	return result


def check_video(src_path, dst_path):
	dir_list = set()
	mismatch_list = []
	with os.scandir(src_path) as it:
		for entry in it:
			if entry.name[0] == '.':
				continue
			if entry.is_dir():
				dir_list.add(entry.name)
				continue
			elif entry.name == "info.xml":
				continue
			else:
				try:
					if filecmp.cmp(os.path.join(src_path, entry.name), os.path.join(dst_path, entry.name), shallow = True):
						continue
				except Exception as e:
					print(e, file = sys.stderr)

				mismatch_list.append(entry.name)

		with open(os.path.join(src_path, "info.json"), "r") as f:
			info = json.load(f)
		for page in info["pages"]:
			part = "P%d" % page["page"]
			cid = str(page["cid"])
			if part in dir_list:
				mismatch_list += check_part(os.path.join(src_path, part), os.path.join(dst_path, cid), part)
				dir_list.discard(part)
			else:
				mismatch_list.append(part)
	mismatch_list += dir_list
	return mismatch_list


with os.scandir(sys.argv[1]) as it:
	for entry in it:
		try:
			if entry.is_dir() and bvid_pattern.fullmatch(entry.name):
				src_path = os.path.join(sys.argv[1], entry.name)
				mismatch_list = check_video(src_path, os.path.join(sys.argv[2], entry.name))
				for item in mismatch_list:
					print(os.path.join(entry.name, item))
				continue
		except Exception as e:
			print(e, file = sys.stderr)
			# raise

		print(entry.name)



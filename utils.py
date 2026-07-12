#!/usr/bin/env python3

import os
import sys
import re
import logging

import constants


UNIT_TABLE = {
	'k': 1000,
	'ki': 0x400,
	'm': 1000 * 1000,
	'mi': 0x100000,
	'g': 1000 * 1000 * 1000,
	'gi': 0x40000000,
}

img_pattern = re.compile(r"^http.?://[^/]+hdslb[.]com/.+/([^/.]+\.[^/.]+)$")
size_pattern = re.compile(r"(\d+)([kKmMgG][Ii]?)?[Bb]?")


def parse_size(size_str):
	match = size_pattern.fullmatch(size_str)
	return int(match.group(1)) * UNIT_TABLE.get(match.group(2).lower(), 1)


def get_relative_path(path, root):
	common_path = os.path.commonpath((path, root))
	if common_path != root:
		return None

	rel_path = os.path.relpath(path, root)
	if ".." in rel_path:
		return None

	assert(rel_path[0] != '/')
	return rel_path


def logger_init(rel_level = 0, /, log_file = None, noprint = False):
	extra_args = {}
	if log_file:
		extra_args = {"filename": log_file}
	elif not noprint:
		extra_args = {"stream": sys.stderr}
	else:
		raise RuntimeError("missing log_file")

	level = logging.INFO - rel_level * 10

	logging.basicConfig(level = level, format = constants.LOG_FORMAT, force = True, **extra_args)
	root_logger = logging.getLogger()

	if (not noprint) and log_file:
		handler = logging.FileHandler(log_file, delay = True)
		handler.setFormatter(logging.Formatter(constants.LOG_FORMAT))
		root_logger.addHandler(handler)

	filter_func = lambda rec: rec.levelno > level or rec.name.startswith("bili_arch")
	for handler in root_logger.handlers:
		handler.addFilter(filter_func)


def list_bv(path):
	bv_list = []
	for f in os.listdir(path):
		if constants.bvid_pattern.fullmatch(f):
			bv_list.append(f)

	return bv_list


def find_images(table):
	if type(table) is dict:
		return find_images(table.values())

	result = {}
	for v in table:
		if type(v) is dict:
			result.update(find_images(v.values()))
		elif type(v) is list:
			result.update(find_images(v))
		elif type(v) is str:
			img_match = img_pattern.fullmatch(v)
			if img_match:
				key = img_match.group(1)
				result[key] = v

	return result

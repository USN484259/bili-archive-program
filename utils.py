#!/usr/bin/env python3

import os
import sys
import re
import logging

import constants

LOG_NOTICE_LEVEL = logging.INFO + 5
LOG_VERBOSE_LEVEL = logging.INFO - 5
LOG_TRACE_LEVEL = logging.DEBUG - 5

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
	try:
		common_path = os.path.commonpath((path, root))
	except ValueError:
		return None

	if common_path != root:
		return None

	rel_path = os.path.relpath(path, root)
	if ".." in rel_path:
		return None

	assert(rel_path and rel_path[0] != '/')
	return rel_path


def logger_init(rel_level = 0, /, log_file = None, noprint = False):
	# inject_custom_levels
	logging.addLevelName(LOG_NOTICE_LEVEL, "NOTICE")
	logging.addLevelName(LOG_VERBOSE_LEVEL, "VERBOSE")
	logging.addLevelName(LOG_TRACE_LEVEL, "TRACE")
	logging.Logger.notice = lambda self, *args, **kwargs: self.log(LOG_NOTICE_LEVEL, *args, **kwargs)
	logging.Logger.verbose = lambda self, *args, **kwargs: self.log(LOG_VERBOSE_LEVEL, *args, **kwargs)
	logging.Logger.trace = lambda self, *args, **kwargs: self.log(LOG_TRACE_LEVEL, *args, **kwargs)

	level = logging.INFO - rel_level * 5
	handlers = []
	if not noprint:
		handlers.append(logging.StreamHandler(stream = sys.stderr))
	if log_file:
		handlers.append(logging.FileHandler(log_file, delay = True))

	logging.basicConfig(level = level, format = constants.LOG_FORMAT, force = True, handlers = handlers)

	root_logger = logging.getLogger()
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

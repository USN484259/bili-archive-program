#!/usr/bin/env python3

import os
import re
import time
import asyncio
import logging
import argparse

import core

# static objects

http_timeout = 20
default_stall_time = 5
bandwidth_limit = None
root_dir = "."
credential = {}

logger = logging.getLogger("bili_arch.runtime")


standard_args = {
	"auth": [
		(("-u", "--credential"), {}),
	],
	"dir": [
		(("-r", "--root"), {}),
		(("-d", "--dir"), {}),
	],
	"network": [
		(("-t", "--timeout"), {"type": int}),
		(("-s", "--stall"), {"type": float}),
	],
	"bandwidth": [
		(("-w", "--bandwidth"), {}),
	],
	"video_mode": [
		(("-m", "--mode"), {"choices" : ["fix", "update", "force"], "default": "fix"}),
	],
	"prefer": [
		(("--prefer",), {}),
		(("--reject",), {}),
	],
}

# helper functions

def load_credential(auth_file):
	global credential
	credential = {}
	parser = re.compile(r"(\S+)[\s\=\:]+(\S+)\s*")
	logger.debug("auth file at %s", auth_file)
	with open(auth_file, "r") as f:
		for line in f:
			match = parser.fullmatch(line)
			if match:
				credential[match.group(1)] = match.group(2)


## startup & runtime

def parse_args(std_args, extra_args = (), *, arg_list = None):
	global http_timeout
	global default_stall_time
	global bandwidth_limit
	global root_dir

	parser = argparse.ArgumentParser()
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-q", "--quiet", action = "count", default = 0)
	parser.add_argument("-l", "--log")

	option_list = []
	for stdarg_name in std_args:
		option_list += standard_args.get(stdarg_name)

	option_list += extra_args

	for args, kwargs in option_list:
		parser.add_argument(*args, **kwargs)

	args = parser.parse_args(arg_list)

	log_level = logging.INFO + 10 * (args.quiet - args.verbose)
	logging.basicConfig(level = log_level, format = core.LOG_FORMAT, force = True)
	root_logger = logging.getLogger()

	if args.log:
		handler = logging.FileHandler(args.log, delay = True)
		handler.setFormatter(logging.Formatter(core.LOG_FORMAT))
		root_logger.addHandler(handler)

	if getattr(args, "stall", None):
		default_stall_time = float(args.stall)

	if getattr(args, "timeout", None):
		http_timeout = int(args.timeout)

	if getattr(args, "bandwidth", None):
		bandwidth_limit = core.number_with_unit(args.bandwidth)

	if getattr(args, "root", None):
		root_dir = args.root

	logger.debug(args)

	# keep credential safe, load after print
	if getattr(args, "credential", None):
		load_credential(args.credential)

	return args


class Stall:
	def __init__(self, stall_time = None):
		global default_stall_time
		self.stall_time = stall_time or default_stall_time
		self.last_time = 0
		self.mutex = asyncio.Lock()

	async def __call__(self):
		async with self.mutex:
			cur_time = time.monotonic()
			time_diff = cur_time - self.last_time
			time_wait = self.stall_time - time_diff
			logger.debug("stall %.1f sec",  max(time_wait, 0))
			if time_diff > 0 and time_wait > 0:
				await asyncio.sleep(time_wait)
				cur_time = time.monotonic()
			self.last_time = cur_time


def subdir(key):
	path = os.path.join(root_dir, key)
	core.mkdir(path)
	return path


def list_bv(path):
	bv_list = []
	for f in os.listdir(path):
		if core.bv_pattern.fullmatch(f):
			bv_list.append(f)

	return bv_list


def report(key, status, *args):
	print(key[0].upper(), status, *args, flush = True)



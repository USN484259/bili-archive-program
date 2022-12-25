import sys
import os
import re
import time
import asyncio
import aiohttp
import traceback
import argparse
import json
from bilibili_api import Credential
from bilibili_api import exceptions as bexp

agent = {
	"User-Agent": "Mozilla/5.0",
	"Referer": "https://www.bilibili.com/"
}

log_prefix = ["FATAL", "ERROR", "WARNING", "INFO", "VERBOSE", "TRACE"]
log_level = 3

stall_mutex = asyncio.Lock()
stall_duration = 1
stall_timestamp = 0

tmp_postfix = None

def do_log(level, msg):
	if level <= log_level:
		print(log_prefix[level], msg, sep = '\t', flush = True)

def logt(msg):
	do_log(5, msg)

def logv(msg):
	do_log(4, msg)

def logi(msg):
	do_log(3, msg)

def logw(msg):
	do_log(2, msg)

def loge(msg):
	do_log(1, msg)

def logf(msg):
	do_log(0, msg)


async def stall():
	global stall_mutex
	global stall_timestamp
	ratio_ns = 1000 * 1000 * 1000

	async with stall_mutex:
		timestamp = time.monotonic_ns()
		time_diff = timestamp - stall_timestamp
		stall_timestamp = timestamp
		time_wait = stall_duration * ratio_ns - time_diff
		logv("stall " + str(max(time_wait, 0)) + "ns")
		if time_diff > 0 and time_wait > 0:
			await asyncio.sleep(time_wait / ratio_ns)


def mkdir(path):
	try:
		logv("mkdir " + path)
		os.mkdir(path)
	except FileExistsError:
		logt("exist" + path)
		pass


def parse_args():
	global log_level
	global stall_duration
	global stall_timestamp
	global tmp_postfix

	parser = argparse.ArgumentParser()
	parser.add_argument("inputs", nargs = '+')
	parser.add_argument("-d", "--dest")
	parser.add_argument("-m", "--mode")
	parser.add_argument("-u", "--auth")
	parser.add_argument("-t", "--stall")
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-q", "--quiet", action = "count", default = 0)
	parser.add_argument("-s", "--stage", action = "store_true")

	args = parser.parse_args()

	log_level = 3 - args.quiet + args.verbose

	if args.stall:
		stall_duration = int(args.stall)
	stall_timestamp = time.monotonic_ns()

	if args.stage:
		tmp_postfix = ".tmp"

	logt(args)
	return args


def opt_path(path):
	if not path:
		return ""
	elif path[-1:] != os.path.sep:
		return path + os.path.sep
	else:
		return path


def handle_exception(e, msg):
	traceback.print_exc()
	print(msg, file = sys.stderr, flush = True)
	if e is bexp.NetworkException and e.status == 412:
		logf("encounter flow control, rethrow")
		raise


def run(func):
	asyncio.get_event_loop().run_until_complete(func)


def credential(auth_file):
	parser = re.compile(r"(\S+)[\s\=\:]+(\S+)\s*")
	info = dict()
	logv("auth file at " + auth_file)
	with open(auth_file, "r") as f:
		for line in f:
			match = parser.fullmatch(line)
			if match:
				logt("got key " + match.group(1))
				info[match.group(1)] = match.group(2)

	return Credential(**info)


async def save_json(obj, path):
	logt("saving json " + path)
	tmp_name = path
	if tmp_postfix:
		tmp_name = path + tmp_postfix
		logv("using stage file " + tmp_name)

	with open(tmp_name, "w") as f:
		json.dump(obj, f, indent = '\t', ensure_ascii = False)

	if tmp_postfix:
		logv("move " + tmp_name + " to " + path)
		os.replace(tmp_name, path)


async def fetch(sess, url, path, mode = "file"):
	logt("fetching " + url + " into " + path)
	async with sess.get(url, headers=agent) as resp:
		logt(resp)
		file_name = path
		length = None
		file_length = None
		file_mode = None
		if mode == "file":
			file_mode = "wb"
			length = int(resp.headers.get('content-length'))
			logt("content length " + str(length))
			if tmp_postfix:
				file_name = path + tmp_postfix
				logv("using stage file " + file_name)
		elif mode == "stream":
			file_mode = "ab"
		else:
			raise Exception("fetch: unknown mode " + mode)

		with open(file_name, file_mode) as f:
			while True:
				chunk = await resp.content.readany()
				# https://stackoverflow.com/questions/56346811/response-payload-is-not-completed-using-asyncio-aiohttp
				# await asyncio.sleep(0)
				if not chunk:
					file_length = f.tell()
					logt("EOF with file length " + str(file_length))
					break
				if 5 <= log_level:
					print(end = '*', flush = True)
				# logt("fetching " + str(f.tell()))
				f.write(chunk)

		if mode == "file":
			if file_length != length:
				logw("file " + file_name + " size mismatch, expect " + str(length) + " got " + str(file_length))
				raise Exception("size mismatch " + path)

			if tmp_postfix:
				logv("move " + file_name + " to " + path)
				os.replace(file_name, path)

		return file_length


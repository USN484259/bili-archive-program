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

sec_to_ns = 1000 * 1000 * 1000

unit_table = {
	'b': 1,
	'B': 1,
	'k': 1000,
	'K': 0x400,
	'm': 1000 * 1000,
	'M': 0x100000,
	'g': 1000 * 1000 * 1000,
	'G': 0x40000000
}

log_prefix = ["FATAL", "ERROR", "WARNING", "INFO", "VERBOSE", "TRACE"]
log_level = 3

stall_mutex = asyncio.Lock()
stall_duration = 1
stall_timestamp = 0

bandwidth_limit = None

tmp_postfix = ".tmp"


def do_log(level, raw, *msg):
	if level > log_level:
		return
	if raw:
		print(*msg, sep = '\t', end = "", flush = True)
	else:
		print(time.strftime("%y-%m-%d %H:%M:%S"), log_prefix[level], *msg, sep = '\t', flush = True)

def logt(*msg, raw = False):
	do_log(5, raw, *msg)

def logv(*msg, raw = False):
	do_log(4, raw, *msg)

def logi(*msg, raw = False):
	do_log(3, raw, *msg)

def logw(*msg, raw = False):
	do_log(2, raw, *msg)

def loge(*msg, raw = False):
	do_log(1, raw, *msg)

def logf(*msg, raw = False):
	do_log(0, raw, *msg)


async def stall():
	global stall_mutex
	global stall_timestamp

	async with stall_mutex:
		timestamp = time.monotonic_ns()
		time_diff = timestamp - stall_timestamp
		time_wait = stall_duration * sec_to_ns - time_diff
		logv("stall " + str(max(time_wait, 0)) + "ns")
		if time_diff > 0 and time_wait > 0:
			await asyncio.sleep(time_wait / sec_to_ns)
			timestamp = time.monotonic_ns()
		stall_timestamp = timestamp


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
	global bandwidth_limit
	global tmp_postfix

	parser = argparse.ArgumentParser()
	parser.add_argument("inputs", nargs = '+')
	parser.add_argument("-d", "--dest")
	parser.add_argument("-m", "--mode")
	parser.add_argument("-u", "--auth")
	parser.add_argument("-t", "--stall")
	parser.add_argument("-v", "--verbose", action = "count", default = 0)
	parser.add_argument("-q", "--quiet", action = "count", default = 0)
	parser.add_argument("-r", "--direct", action = "store_true")
	parser.add_argument("-w", "--bandwidth")

	args = parser.parse_args()

	log_level = 3 - args.quiet + args.verbose

	if args.stall:
		stall_duration = int(args.stall)
	stall_timestamp = time.monotonic_ns()

	if args.bandwidth:
		match = re.fullmatch(r"(\d+)([bBkKmMgG]?)", args.bandwidth)
		bandwidth_limit = int(match.group(1)) * unit_table.get(match.group(2), 'b')

	if args.direct:
		tmp_postfix = None

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


class session(aiohttp.ClientSession):
	def __init__(self):
		super(session, self).__init__(timeout = aiohttp.ClientTimeout())


async def fetch(sess, url, path, mode = "file"):
	logt("fetching " + url + " into " + path)
	async with sess.get(url, headers=agent) as resp:
		logt(resp)
		if not resp.ok:
			raise Exception("HTTP " + str(resp.status) + '\t' + resp.reason)

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
			if mode == "file" and bandwidth_limit:
				logv("bandwidth limit " + str(bandwidth_limit) + " byte/sec")
				last_timestamp = time.monotonic_ns()

			while True:
				chunk = await resp.content.readany()
				if not chunk:
					file_length = f.tell()
					logt("EOF with file length " + str(file_length))
					break

				logt('*', raw = True)
				f.write(chunk)

				if mode == "file" and bandwidth_limit:
					cur_timestamp = time.monotonic_ns()
					time_diff = cur_timestamp - last_timestamp
					expect_time = sec_to_ns * len(chunk) / bandwidth_limit
					time_wait = expect_time - time_diff
					if time_diff > 0 and time_wait > 0:
						logt('<' + str(time_wait) + "ns>", raw = True)
						await asyncio.sleep(time_wait / sec_to_ns)
						cur_timestamp = time.monotonic_ns()
					last_timestamp = cur_timestamp


		if mode == "file":
			if file_length != length:
				logw(file_name, "size mismatch, expect " + str(length) + " got " + str(file_length))
				raise Exception("size mismatch " + path)

			if tmp_postfix:
				logv("move " + file_name + " to " + path)
				os.replace(file_name, path)

		return file_length


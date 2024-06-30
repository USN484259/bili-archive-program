#!/usr/bin/env python3

import os
import time
import asyncio
import zipfile
import logging

import core
import hls

# constants

LIVE_STAT_STALL_TIME = 2
LIVE_STAT_RETRY_COUNT = 5

DEFAULT_PREFER = "ts flv avc"
DEFAULT_REJECT = "hevc"

LIVE_QUERY_URL = "https://api.live.bilibili.com/room/v1/Room/get_info"
LIVE_USER_URL = "https://api.live.bilibili.com/live_user/v1/Master/info"
LIVE_PLAY_URL = "https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"

# static objects

name_escape_table = str.maketrans({
	'/':	'-',
	'\\':	'-',
	'.':	'_'
})

logger = logging.getLogger("bili_arch.live_rec")

# helper functions

def get_url_path(base_url):
	index = base_url.rfind('/')
	return base_url[0:index + 1]


async def get_live_info(sess, rid):
	info = await core.request(sess, "GET", LIVE_QUERY_URL, params = {"room_id" : rid})
	return info


async def get_user_info(sess, uid):
	info = await core.request(sess, "GET", LIVE_USER_URL, params = {"uid": uid})
	return info


async def get_live_url(sess, rid):
	params = {
		"room_id": rid,
		"protocol": "0,1",
		"format": "0,1,2",
		"codec": "0,1",
		"qn": 10000
	}
	info = await core.request(sess, "GET", LIVE_PLAY_URL, params = params)
	return info


async def fetch_stream(sess, url, sink_func, *args):
	sink = None
	if not callable(sink_func):
		sink = sink_func

	try:
		async with sess.stream("GET", url) as resp:
			logger.debug(resp)
			resp.raise_for_status()

			async for chunk in resp.aiter_bytes():
				if not sink:
					logger.debug("calling sink_func")
					sink = sink_func(*args)

				sink.write(chunk)
	finally:
		if callable(sink_func) and sink:
			logger.debug("closing sink")
			sink.close()


def find_best_url(info, *, prefer = "", reject = ""):
	# preference priority low-to-high
	url_streams = info.get("playurl_info").get("playurl").get("stream")
	result = None
	result_score = None
	for stream in url_streams:
		for fmt in stream.get("format"):
			for codec in fmt.get("codec"):
				logger.debug("protocol %s, format %s, codec %s, qn %d", stream.get("protocol_name"), fmt.get("format_name"), codec.get("codec_name"), codec.get("current_qn"))
				if fmt.get("format_name") in reject:
					continue
				if codec.get("codec_name") in reject:
					continue
				if result and result.get("current_qn") >= codec.get("current_qn"):
					if result.get("current_qn") > codec.get("current_qn"):
						continue
					score = prefer.find(fmt.get("format_name")) + prefer.find(codec.get("codec_name"))
					if score <= result_score:
						continue

				result = codec.copy()
				result["protocol_name"] = stream.get("protocol_name")
				result["format_name"] = fmt.get("format_name")
				result_score = prefer.find(result.get("format_name")) + prefer.find(result.get("codec_name"))

	if result:
		logger.debug("best: protocol %s, format %s, codec %s, qn %d", result.get("protocol_name"), result.get("format_name"), result.get("codec_name"), result.get("current_qn"))
	return result


# methods

def make_record_name(uname, title):
	return (uname + '_' + title + '_').translate(name_escape_table) + time.strftime("%y_%m_%d_%H_%M")


async def record_flv(sess, info, name_prefix):
	file_name = name_prefix + ".flv"
	connected = False

	def on_file_open(*args):
		connected = True
		return core.locked_file(*args)

	for url_info in info.get("url_info"):
		try:
			url = url_info.get("host") + info.get("base_url") + url_info.get("extra")
			await fetch_stream(sess, url, on_file_open, file_name, "xb")
		except Exception:
			logger.exception("exception on record_flv")
			if connected:
				raise
	else:
		raise Exception("record_flv: no valid URL")


async def record_hls(sess, info, name_prefix):
	url_info_list = info.get("url_info")
	last_url_index = 0
	cur_url_index = 0
	with core.locked_file(name_prefix + ".zip", "x+b") as zip_file:
		with zipfile.ZipFile(zip_file, mode = "w") as archive:
			async def req_func(buffer, url, sess):
				return await fetch_stream(sess, url, buffer)

			m3u = hls.M3u()
			stall = None
			try:
				while True:
					url_info = url_info_list[cur_url_index]
					url_path = get_url_path(info.get("base_url"))
					idx_url = url_info.get("host") + info.get("base_url") + url_info.get("extra")
					try:
						res_list = await m3u.async_update(req_func, idx_url, sess)
						if res_list is None:
							break
						file_time = time.gmtime()
						for name in res_list:
							file_info = zipfile.ZipInfo(name, file_time)
							url = url_info.get("host") + url_path + name + '?' + url_info.get("extra")
							logger.debug("%s: %s", name, url)
							await fetch_stream(sess, url, archive.open, file_info, "w")

						last_url_index = cur_url_index
						if not stall:
							stall = core.Stall(m3u.duration)

					except Exception as e:
						logger.exception("exception on fetching index")
						cur_url_index += 1
						cur_url_index %= len(url_info_list)
						if cur_url_index == last_url_index:
							raise
						else:
							continue

					await stall()

			finally:
				file_time = time.gmtime()
				file_info = zipfile.ZipInfo(core.default_names.hls_index, file_time)
				with archive.open(file_info, "w") as f:
					m3u.dump(f)


async def record(sess, rid, path, *, prefer = None, reject = None):
	try:
		logger.debug("record live %d into %s", rid, path)
		if prefer is None:
			prefer = DEFAULT_PREFER
		if reject is None:
			reject = DEFAULT_REJECT
		logger.debug("prefer %s, reject %s", prefer, reject)

		stat_fail_count = 0
		stall = core.Stall(LIVE_STAT_STALL_TIME)
		while True:
			start_time = time.time()
			info = None
			try:
				info = await get_live_url(sess, rid)
				live_status = info.get("live_status")
				logger.info("room %d, status %d", rid, live_status)
				stat_fail_count = 0

				if live_status != 1:
					break
			except Exception as e:
				logger.exception("exception on checking live status")
				stat_fail_count += 1
				if stat_fail_count > LIVE_STAT_RETRY_COUNT:
					logger.error("live status fail %d times", stat_fail_count)
					raise

			try:
				name_prefix = os.path.join(path, str(int(start_time)))

				url_info = find_best_url(info, prefer = prefer, reject = reject)
				if not url_info:
					norej_info = find_best_url(info, prefer = prefer)
					if norej_info:
						logger.warning("bad reject: %s", reject)
						url_info = norej_info

				if "hls" in url_info.get("protocol_name"):
					await record_hls(sess, url_info, name_prefix)
				else:
					await record_flv(sess, url_info, name_prefix)

			except Exception as e:
				logger.exception("exception on recording")

			await stall()

	except Exception as e:
		logger.exception("exception in record")
		raise


# entrance

async def main(args):
	live_root = args.dir or core.subdir("live")
	user_info = {}
	async with core.session(args.credential) as sess:
		while True:
			try:
				info = await get_live_info(sess, args.room)
				uid = info.get("uid")
				status = info.get("live_status")
				logger.info("room %d, status %d", info.get("room_id"), status)

				if not user_info or status == 1:
					try:
						user_info = (await get_user_info(sess, uid)).get("info")
					except Exception:
						logger.exception("exception on getting user info")

				if status == 1:
					rec_name = make_record_name(user_info.get("uname", str(uid)), info.get("title"))
					with core.locked_path(live_root, rec_name) as rec_path:
						await record(sess, args.room, rec_path, prefer = args.prefer, reject = args.reject)

			except Exception:
				logger.exception("exception on checking")

			if not args.monitor:
				logger.info("room %d not streaming, exit", args.room)
				return

			logger.info("checking done, sleep %d sec", args.interval)
			await asyncio.sleep(args.interval)


if __name__ == "__main__":
	args = core.parse_args([
		(("room",), {"type" : int}),
		(("-d", "--dir"), {}),
		(("-i", "--interval"), {"type" : int, "default" : 30}),
		(("--monitor",),{"action" : "store_true"}),
		(("--prefer",), {}),
		(("--reject",), {})
	])
	asyncio.run(main(args))

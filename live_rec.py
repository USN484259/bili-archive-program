#!/usr/bin/env python3

import os
import io
import time
import asyncio
import re
import json
import zipfile
import logging
from collections import deque
import bilibili_api
from bilibili_api import live, user
import util

logger = logging.getLogger("bili_arch.live_rec")


async def fetch_stream(url, sink_func, *args):
	logger.debug("start fetching stream %s", url)
	sess = bilibili_api.get_session()

	if callable(sink_func):
		sink = None
	else:
		sink = sink_func

	try:
		async with sess.stream("GET", url, headers = util.agent, timeout = util.http_timeout) as resp:
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


async def worker_danmaku(room, credential, path):
	try:
		rec_name = os.path.join(path, "danmaku.json")
		logger.debug("record live danmaku into " + rec_name)
		conn = live.LiveDanmaku(room.room_display_id, credential = credential)
		with util.locked_file(rec_name, "a") as f:
			mutex = asyncio.Lock()
			queue = deque()
			cond = asyncio.Condition()
			async def worker_wget(path):
				try:
					while True:
						name = None
						url = None
						async with cond:
							logger.debug("wget standby")
							await cond.wait()
							name, url = queue.popleft()

						logger.info("fetch emot " + name)
						ext = os.path.splitext(url)[1]
						if not ext or ext == "":
							ext = ".png"
						emot_file = os.path.join(path, name + ext)
						if os.path.isfile(emot_file):
							logger.debug("skip existing emot")
						else:
							await util.fetch(url, emot_file)
				except Exception as e:
					logger.exception("exception in worker_wget")
					raise


			async def on_event(info):
				cmd = info.get("type")
				data = info.get("data")
				if not data:
					data = {}
				elif not isinstance(data, dict):
					data = { "data": data }

				data["cmd"] = cmd
				data["timestamp"] = int(time.time_ns() / util.sec_to_ns)

				async with mutex:
					logger.debug(cmd)
					f.write(json.dumps(data, ensure_ascii = False) + '\n')

				for meta in data.get("info", [[]])[0]:
					if not isinstance(meta, dict):
						continue
					emot_name = meta.get("emoticon_unique")
					emot_url = meta.get("url")
					if emot_name and emot_url:
						logger.debug("scheduled emot fetch " + emot_name)
						async with cond:
							queue.append((emot_name, emot_url))
							cond.notify()

			task_wget = asyncio.create_task(worker_wget(path))
			# task_wget.add_done_callback(asyncio.Task.result)
			conn.add_event_listener("ALL", on_event)

			while True:
					try:
						logger.debug("connect to live danmaku")
						await conn.connect()
					except Exception as e:
						logger.exception("exception on recording danmaku")

	except Exception as e:
		logger.exception("exception in worker_danmaku")
		raise

	finally:
		logger.debug("disconnect live danmaku")
		task_wget.cancel()
		await conn.disconnect()
		try:
			await task_wget
		except asyncio.CancelledError:
			pass


async def worker_record(room, path):
	try:
		logger.debug("record live into %s", path)

		first_time = True
		while True:
			start_time = time.time_ns()
			try:
				if first_time:
					first_time = False
				else:
					play_info = await room.get_room_play_info()
					status = play_info.get("live_status")
					logger.info("room %d, status %d", play_info.get("room_id"), status)
					if status != 1:
						break

				url_info = await get_url(room)
				logger.debug(url_info)
				name_prefix = os.path.join(path, str(start_time // util.sec_to_ns))
				if url_info.get("file") == "index.m3u8":
					await record_hls(name_prefix + ".zip", url_info)
				else:
					file_url = url_info.get("host") + url_info.get("base") + url_info.get("extra")
					await record_flv(name_prefix + ".flv", file_url)


			except Exception as e:
				logger.exception("exception on recording")

			stop_time = time.time_ns()
			diff_time = stop_time - start_time
			sleep_time = util.sec_to_ns - diff_time
			if diff_time >= 0 and sleep_time > 0:
				logger.debug("restart record after %d ns", sleep_time)
				await asyncio.sleep(sleep_time / util.sec_to_ns)

	except Exception as e:
		logger.exception("exception in worker_record")
		raise


async def get_url(room):
	info = await room.get_room_play_info_v2()

	url_table = info.get("playurl_info").get("playurl").get("stream")[0].get("format")[0].get("codec")[0]

	logger.debug(url_table)

	qn_table = url_table.get("accept_qn")
	best_qn = 0
	for i in range(len(qn_table)):
		if qn_table[i] > qn_table[best_qn]:
			best_qn = i

	logger.info("best qn %d, index %d", qn_table[best_qn], best_qn)

	url_info = url_table.get("url_info")[best_qn]

	url_match = re.fullmatch(r"(.+/)(.+)\?", url_table.get("base_url"))
	url_path = url_match.group(1)
	url_file = url_match.group(2)

	return {
			"host": url_info.get("host"),
			"base": url_table.get("base_url"),
			"path": url_path,
			"file": url_file,
			"extra": url_info.get("extra"),
		}


async def record_flv(flv_name, url):
	await fetch_stream(url, util.locked_file, flv_name, "xb")

async def record_hls(zip_name, url_info):
	with util.locked_file(zip_name, "x+b") as zip_file:
		with zipfile.ZipFile(zip_file, mode = 'w') as archive:
			end_list = False
			while not end_list:
				index_buffer = io.BytesIO()
				index_url = url_info.get("host") + url_info.get("base") + url_info.get("extra")
				await util.stall(2)
				await fetch_stream(index_url, index_buffer)
				index_buffer.seek(0)
				index_file = io.TextIOWrapper(index_buffer)
				while True:
					line = index_file.readline()
					logger.info(line)
					if not line:
						break
					line = line.strip()
					if line == "#EXT-X-ENDLIST":
						logger.info("end-list, exiting")
						end_list = True
						continue

					name = None
					match = re.fullmatch(r'#EXT-X-MAP:URI="(.+)"', line)
					if match:
						name = match.group(1)
					elif not line.startswith("#"):
						name = line

					if not name:
						continue
					try:
						archive.getinfo(name)
						logger.debug("skip existing file %s", name)
						continue
					except KeyError:
						pass

					url = url_info.get("host") +  url_info.get("path") + name + '?' + url_info.get("extra")

					file_info = zipfile.ZipInfo(name, time.gmtime())
					logger.debug("save file %s, url %s", name, url)
					await fetch_stream(url, archive.open, file_info, "w")


async def record(rid, credential, live_root, uname, title):
	rec_name = uname + '_' + title + '_' + time.strftime("%y_%m_%d_%H_%M")
	logger.info("recording liveroom %d, title %s, path %s", rid, title, live_root)

	room = live.LiveRoom(rid, credential)
	play_info = await room.get_room_play_info()
	status = play_info.get("live_status")
	logger.info("room %d, status %d", play_info.get("room_id"), status)
	if status != 1:
		logger.warning("not streaming, exit")
		return

	with util.locked_path(live_root, rec_name) as rec_path:
		task_danmaku = None
		if credential:
			task_danmaku = asyncio.create_task(worker_danmaku(room, credential, rec_path))
			task_danmaku.add_done_callback(asyncio.Task.result)
		else:
			logger.warning("missing credential, skip recording danmaku")

		try:
			await worker_record(room, rec_path)
		finally:
			if task_danmaku is not None:
				task_danmaku.cancel()
				try:
					await task_danmaku
				except asyncio.CancelledError:
					pass


async def main(args):
	credential = None
	if args.auth:
		credential = await util.credential(args.auth)
	room = live.LiveRoom(args.room, credential)
	room_info = (await room.get_room_info()).get("room_info")
	usr = user.User(room_info.get("uid"))
	user_info = await usr.get_user_info()
	live_root = args.dir or util.subdir("live")

	await record(args.room, credential, live_root, user_info.get("name"), room_info.get("title"))


if __name__ == "__main__":
	args = util.parse_args([
		(("room",), {"type" : int}),
		(("-d", "--dir"), {}),
		(("-u", "--auth"), {}),
	])
	util.run(main(args))

#!/usr/bin/env python3

import os
import re
import json
import shutil
import subprocess
import util
from interactive import is_interactive

try:
	from defusedxml.sax import make_parser as xml_make_parser
except ModuleNotFoundError:
	from xml.sax import make_parser as xml_make_parser

ffprobe_bin = shutil.which("ffprobe")
ffprobe_options = [
	"-hide_banner",
	"-show_format",
	"-show_streams",
	"-print_format", "json=compact=1"
]

subtitle_pattern = re.compile(r"subtitle\.(.*)\.json")
xml_parser = xml_make_parser()


def ffprobe(path):
	result = {}
	try:
		util.logv("running ffprobe on " + path)
		cmdline = [ffprobe_bin]
		if util.log_level <= 3:
			cmdline = cmdline + ["-v", "8"]

		cmdline = cmdline + ffprobe_options + [path]
		util.logt(cmdline)
		with subprocess.Popen(cmdline, stdout = subprocess.PIPE) as proc:
			result = json.load(proc.stdout)
			util.logt(result)

	except Exception as e:
		util.handle_exception(e, "exception on ffprobe for " + path)

	return result


def verify_media(path, part_duration, duration_tolerance):
	media_info = ffprobe(path)
	try:
		vc = 0
		ac = 0
		for i, media in enumerate(media_info.get("streams")):
			media_type = media.get("codec_type")
			media_duration = media.get("duration", None)
			if not media_duration:
				util.logw("missing duration in stream-info, use format-info")
				media_duration = media_info.get("format").get("duration")
			media_duration = float(media_duration)

			util.logv("stream " + str(media.get("index")), "type " + media_type, "duration " + str(media_duration))

			if not duration_tolerance:
				util.logv("skip duration check")
			elif not part_duration:
				util.logw("unknown duration, skip")
			elif abs(part_duration - media_duration) >= duration_tolerance:
				util.logw("media duration mismatch")
				util.logv("duration",  '(' + str(part_duration) + '/' + str(media_duration) + ')')
				return 0, 0

			if media_type == "video":
				vc += 1
			elif media_type == "audio":
				ac += 1

		return vc, ac
	except Exception as e:
		util.handle_exception(e, "exception on verify media for " + path)
		return 0, 0


def verify_bv(bv, path = None, scan_files = False, duration_tolerance = None):
	if scan_files and not ffprobe_bin:
		raise Exception("ffprobe binary not found")

	result = {
		"info" : False,
		"cover" : False
	}
	try:
		bv_root = util.opt_path(path) + bv + os.path.sep
		util.logv("verify video " + bv, "path " + bv_root, "scan " + str(scan_files), "tolerance " + str(duration_tolerance))

		util.logv("loading video info")
		with open(bv_root + "info.json") as f:
			bv_info = json.load(f)
			util.logt(bv_info)

		util.logi(bv, bv_info.get("title"))

		for ext in [".jpg", ".png", ".gif", ".bmp"]:
			cover_file = bv_root + "cover" + ext
			util.logv("checking cover " + cover_file)
			if not os.path.isfile(cover_file):
				continue
			if scan_files:
				media_info = ffprobe(cover_file)
				try:
					codec = media_info.get("streams")[0].get("codec_name")
					util.logv("cover codec " + codec)
					if codec not in ["mjpeg", "png"]:
						continue
				except Exception as e:
					util.handle_exception("exception on verify cover for " + cover_file)
					continue

			util.logv("found cover, format " + ext)
			result["cover"] = True
			break
		else:
			util.logw("cover not found")

		part_list = None
		if "interactive" in bv_info:
			if not is_interactive(bv_info):
				raise Exception("conflicting interactive video status")

			part_list = bv_info.get("interactive").get("nodes")

			util.logv("interactive video, checking graph")
			# TODO scan DOT file
			result["graph"] = os.path.isfile(bv_root + "graph.dot")
		else:
			part_list = bv_info.get("pages")
			if bv_info.get("videos") != len(part_list):
				raise Exception("part count mismatch")

		for part in part_list:
			cid = part.get("cid")
			part_duration = part.get("duration", None)

			util.logi("cid " + str(cid), part.get("part", None) or part.get("title", ""), str(part_duration or "??") + " sec")

			video_name = "V:" + str(cid)
			danmaku_name = "D:" + str(cid)
			subtitle_name = "S:" + str(cid)
			result[video_name] = False
			result[danmaku_name] = False

			subtitle = bv_info.get("subtitle")[str(cid)].get("subtitles")
			util.logv("subtitle count " + str(len(subtitle)))
			util.logt(subtitle)
			if len(subtitle) > 0:
				result[subtitle_name] = False

			part_root = bv_root + str(cid) + os.path.sep

			if not os.path.isdir(part_root):
				util.logv("part dir not exist")
				continue

			video_count = 0
			audio_count = 0
			subtitle_count = 0
			no_audio = False

			util.logv("checking " + str(cid) + " in " + part_root)
			for filename in os.listdir(part_root):
				util.logv("file " + filename)
				ext = os.path.splitext(filename)[1].lower()

				if ext == util.tmp_postfix:
					util.logv("skip tmp file")
					continue

				if filename == util.noaudio_stub:
					util.logw("found no-audio stub")
					no_audio = True
					continue

				if filename == "danmaku.xml":
					util.logv("type: danmaku")
					if scan_files:
						try:
							with open(part_root + filename, "r") as f:
								xml_parser.parse(f)
						except:
							util.logw("unexpected XML format")
							continue

					util.logv("found danmaku")
					result[danmaku_name] = True
					continue

				if ext == ".json":
					match = subtitle_pattern.fullmatch(filename)
					if not match:
						util.logw("type: json (unknown)")
						continue

					lan = match.group(1)
					util.logv("type: subtitle " + lan)
					if scan_files:
						try:
							with open(part_root + filename, "r") as f:
								json.load(f)
						except:
							util.logw("unexpected JSON format")
							continue

					util.logv("found subtitle, lang " + lan)
					for i, sub in enumerate(subtitle):
						if sub.get("lan") == lan:
							del subtitle[i]
							subtitle_count += 1
							break
					else:
						util.logw("subtitle not recorded " + lan)

					continue

				if ext == ".m4v":
					util.logv("type: video")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration, duration_tolerance)
						if vc != 1 or ac != 0:
							util.logw("unexpected media streams")
							continue

					util.logv("found video")
					video_count += 1
					continue

				if ext == ".m4a":
					util.logv("type: audio")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration, duration_tolerance)
						if vc != 0 or ac != 1:
							util.logw("unexpected media streams")
							continue

					util.logv("found audio")
					audio_count += 1
					continue

				if ext == ".flv":
					util.logv("type: flv")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration, duration_tolerance)
						if vc != 1 or ac != 1:
							util.logw("unexpected media streams")
							continue

					util.logv("found video & audio")
					video_count += 1
					audio_count += 1
					continue

				util.logv("unknown type " + ext)

			util.logv("video count " + str(video_count), "audio_count " + str(audio_count), "subtitle_count " + str(subtitle_count))
			if video_count > 0 and (no_audio or audio_count > 0):
				result[video_name] = True
			else:
				util.logw("missing media files")

			if not result[danmaku_name]:
				util.logw("missing danmaku file")

			if subtitle_name in result:
				if len(subtitle) == 0:
					result[subtitle_name] = True
				else:
					util.logw("missing subtitle " + str(len(subtitle)) + '/' + str(subtitle_count))
					util.logt(subtitle)

		util.logv("verify done for " + bv)
		result["info"] = True
	except Exception as e:
		util.handle_exception(e, "exception in verifing " + bv)

	util.logi(result)
	return result

def main(args):
	if len(args.inputs) > 0:
		util.logv(str(len(args.inputs)) + " BV on cmdline")
		bv_list = args.inputs
	else:
		util.logv("scan BV in " + (args.dest or "(cwd)"))
		bv_list = util.list_bv(args.dest)

	util.logi("BV count " + str(len(bv_list)))
	util.logt(bv_list)

	tolerance = args.tolerance
	if tolerance:
		tolerance = float(tolerance)

	for i, bv in enumerate(bv_list):
		result = verify_bv(bv, path = args.dest, scan_files = args.scan, duration_tolerance = tolerance)
		res = True
		for k, v in result.items():
			if not v:
				res = False
				break

		print(bv, res, flush = True)

if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '*'}),
		(("--scan",), {"action" : "store_true"}),
		(("--tolerance",), {"type" : float})
	])
	main(args)

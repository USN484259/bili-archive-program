#!/usr/bin/env python3

import os
import re
import util
import json
import shutil
import subprocess

try:
	from defusedxml.sax import make_parser as xml_make_parser
except ModuleNotFoundError:
	from xml.sax import make_parser as xml_make_parser

ffprobe_path = shutil.which("ffprobe")
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
		cmdline = [ffprobe_path]
		if util.log_level <= 3:
			cmdline = cmdline + ["-v", "8"]

		cmdline = cmdline + ffprobe_options + [path]
		util.logv("running ffprobe on " + path)
		util.logt(cmdline)
		with subprocess.Popen(cmdline, stdout = subprocess.PIPE) as proc:
			result = json.load(proc.stdout)
			util.logt(result)

	except Exception as e:
		util.handle_exception(e, "exception on ffprobe for " + path)

	return result


def verify_media(path, part_duration, duration_tolerance = 2):
	media_info = ffprobe(path)
	try:
		vc = 0
		ac = 0
		for i, media in enumerate(media_info.get("streams")):
			media_type = media.get("codec_type")
			media_duration = float(media.get("duration"))
			util.logv("stream " + str(media.get("index")), "type " + media_type, "duration " + str(media_duration))

			if abs(part_duration - media_duration) >= duration_tolerance:
				util.logw("media duration mismatch, skipping", f)
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


def verify_bv(bv, path = None, scan_files = False):
	result = {
		"info" : False,
		"cover" : False
	}
	try:
		bv_root = util.opt_path(path) + bv + os.path.sep
		util.logv("verify video " + bv, "path " + bv_root, "scan_files " + str(scan_files))

		with open(bv_root + "info.json") as f:
			bv_info = json.load(f)
			util.logt(bv_info)

		util.logi(bv, bv_info.get("title"))


		if scan_files:
			util.logv("checking cover")
			media_info = ffprobe(bv_root + "cover.jpg")
			try:
				codec = media_info.get("streams")[0].get("codec_name")
				util.logv("cover format " + codec)
				if codec in ["mjpeg", "png"]:
					result["cover"] = True
			except:
				pass

		elif os.path.isfile(bv_root + "cover.jpg"):
			util.logv("cover exists")
			result["cover"] = True

		for i in range(bv_info.get("videos")):
			part_name = 'P' + str(i + 1)
			danmaku_name = 'D' + str(i + 1)
			subtitle_name = 'S' + str(i + 1)
			result[part_name] = False
			result[danmaku_name] = False

			subtitle = bv_info.get("subtitle")[i].get("subtitles")
			if len(subtitle) > 0:
				result[subtitle_name] = False

			part_info = bv_info.get("pages")[i]
			part_duration = part_info.get("duration")
			util.logi(part_name, part_info.get("part"), str(part_duration) + " sec")

			part_root = bv_root + part_name + os.path.sep

			if not os.path.isdir(part_root):
				util.logv("part dir not exist")
				continue

			video_count = 0
			audio_count = 0
			subtitle_count = 0
			no_audio = False

			util.logv("checking " + part_name + " in " + part_root)
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

					for i, sub in enumerate(subtitle):
						if sub.get("lan") == lan:
							del subtitle[i]
							subtitle_count = subtitle_count + 1
							break
					else:
						util.logw("subtitle not recorded " + lan)

					continue

				if ext == ".m4v":
					util.logv("type: video")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration)
						if vc != 1 or ac != 0:
							util.logw("unexpected media streams")
							continue
					video_count += 1
					continue

				if ext == ".m4a":
					util.logv("type: audio")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration)
						if vc != 0 or ac != 1:
							util.logw("unexpected media streams")
							continue

					audio_count += 1
					continue

				if ext == ".flv":
					util.logv("type: flv")
					if scan_files:
						vc, ac = verify_media(part_root + filename, part_duration)
						if vc != 1 or ac != 1:
							util.logw("unexpected media streams")
							continue

					video_count += 1
					audio_count += 1
					continue

				util.logv("unknown type " + ext)

			util.logv("video count " + str(video_count), "audio_count " + str(audio_count), "subtitle_count " + str(subtitle_count))
			if video_count > 0 and (no_audio or audio_count > 0):
				result[part_name] = True
			if len(subtitle) == 0:
				result[subtitle_name] = True

		util.logv("verify done " + bv)
		result["info"] = True
	except Exception as e:
		util.handle_exception(e, "exception in verifing " + bv)

	util.logv(result)
	return result

def main(args):
	if len(args.inputs) > 0:
		util.logv(str(len(args.inputs)) + " BV on cmdline")
		bv_list = args.inputs
	else:
		util.logv("scan BV in " + (args.dest or "(cwd)"))
		bv_list = util.list_bv(args.dest)

	util.logi("BV count " + str(len(bv_list)), "mode " + str(args.mode))
	util.logt(bv_list)

	for i, bv in enumerate(bv_list):
		result = verify_bv(bv, path = args.dest, scan_files = args.scan)
		res = True
		for k, v in result.items():
			if not v:
				res = False
				break

		print(bv, res, flush = True)

if __name__ == "__main__":
	args = util.parse_args([
		(("--scan",), {"action" : "store_true"})
	])
	main(args)

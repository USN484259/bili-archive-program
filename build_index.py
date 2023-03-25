#!/usr/bin/env python3

import os
import sys
import json
import yattag
import util

search_script = """
search_label = document.getElementById("search_label")
search_box = document.getElementById("search_box")
search_button = document.getElementById("search_button")
listing = document.getElementById("listing")

function on_click() {
	search_text = search_box.value.toLocaleLowerCase()
	total = 0
	shown = 0
	for (element of listing.children) {
		text = element.getElementsByClassName("title")[0].innerHTML
		if (text.toLocaleLowerCase().includes(search_text)) {
			shown += 1
			element.removeAttribute("hidden")
		} else {
			element.setAttribute("hidden", "")
		}
		total += 1
	}
	search_label.innerHTML = shown.toString() + '/' + total.toString()

}

search_button.addEventListener("click", on_click)

"""

def build_video_index(path, img_size = 64):
	path = util.opt_path(path)
	util.logi("building video index for", path)
	bv_list = util.list_bv(path)
	util.logt(bv_list)
	util.logv("found " + str(len(bv_list)) + " videos")

	doc = yattag.Doc()
	doc.asis("<!DOCTYPE html>")
	with doc.tag("html"):
		with doc.tag("head"):
			doc.stag("meta", charset="UTF-8")
			pass
		with doc.tag("body"):
			with doc.tag("div"):
				doc.stag("input", type="search", id="search_box")
				doc.line("button", "search", id="search_button")
				doc.line("label", "", ("for", "search_box"), ("id", "search_label"))
			with doc.tag("table"):
				with doc.tag("thead"):
					with doc.tag("tr"):
						doc.line("th", "BV")
						doc.line("th", "cover")
						doc.line("th", "author")
						doc.line("th", "title")

				with doc.tag("tbody", id="listing"):
					for bv in bv_list:
						info = None
						with open(path + bv + os.path.sep + "info.json") as f:
							info = json.load(f)

						bv_root = "/video/" + bv + '/'
						cover_ext = ".jpg"
						for ext in [".jpg", ".png", ".gif", ".bmp"]:
							if os.path.isfile(path + os.path.sep + bv + os.path.sep  + "cover" + ext):
								cover_ext = ext
								break

						with doc.tag("tr"):
							with doc.tag("td"):
								doc.line("a", bv, href=bv_root)

							with doc.tag("td"):
								doc.stag("img", src=bv_root + "cover" + cover_ext, loading="lazy", height=img_size)
							with doc.tag("td"):
								doc.line("span", info.get("owner", {}).get("name"))
							with doc.tag("td", klass="title"):
								doc.line("span", info.get("title"))

			with doc.tag("script"):
				doc.asis(search_script)

	return doc.getvalue()


def main(args):
	out = sys.stdout
	util.logv("output to " + (args.out or "stdout"))
	if args.out:
		out = open(args.out, "w")

	out.write(build_video_index(util.opt_path(args.dest) +  "video"))
	out.close()


if __name__ == "__main__":
	args = util.parse_args([
		(("inputs",), {"nargs" : '*'}),
		(("-o", "--out"), {})
	])

	main(args)


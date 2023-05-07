#!/usr/bin/env python3

import os
import sys
import json
import yattag
import logging
import util

logger = logging.getLogger(__name__)

search_script = """
search_label = document.getElementById("search_label")
search_box = document.getElementById("search_box")
search_title = document.getElementById("search_title")
search_owner = document.getElementById("search_owner")
listing = document.getElementById("listing")

function apply_filter() {
	search_text = search_box.value.toLocaleLowerCase()
	total = 0
	shown = 0
	check_title = search_title.checked
	check_owner = search_owner.checked
	for (element of listing.children) {
		check_result = false

		if (check_title) {
			text = element.getElementsByClassName("title")[0].children[0].innerHTML
			if (text.toLocaleLowerCase().includes(search_text)) {
				check_result = true
			}
		}
		if (check_owner) {
			owner_element = element.getElementsByClassName("owner")[0]
			if (owner_element.children[0].tagName == "ul") {
				owner_element = owner_element.children[0]
			}
			for (const elem of owner_element.children) {
				name = elem.innerHTML
				if (name.toLocaleLowerCase().includes(search_text)) {
					check_result = true
					break
				}
			}
		}
		if (check_result) {
			shown += 1
			element.removeAttribute("hidden")
		} else {
			element.setAttribute("hidden", "")
		}
		total += 1
	}
	search_label.innerHTML = shown.toString() + '/' + total.toString()

}


search_box.addEventListener("keydown", (ev) => {
	if (ev.keyCode === 13) {
		apply_filter()
	}
})

"""

def build_video_index(path, img_size = 64):
	path = util.opt_path(path)
	logger.info("building video index for " + path)
	bv_list = util.list_bv(path)
	logger.log(util.LOG_TRACE, bv_list)
	logger.debug("found %d videos", len(bv_list))

	doc = yattag.Doc()
	doc.asis("<!DOCTYPE html>")
	with doc.tag("html"):
		with doc.tag("head"):
			doc.stag("meta", charset="UTF-8")
			pass
		with doc.tag("body"):
			with doc.tag("div"):
				with doc.tag("p"):
					doc.stag("input", type="search", id="search_box", autofocus=True)
					doc.line("label", "", ("for", "search_box"), ("id", "search_label"))
				with doc.tag("p"):
					doc.stag("input", type="checkbox", id="search_title", checked=True)
					doc.line("label", "title", ("for", "search_title"))
					doc.stag("br")
					doc.stag("input", type="checkbox", id="search_owner", name="owner", checked=False)
					doc.line("label", "owner", ("for", "search_owner"))
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

							with doc.tag("td", klass="owner"):
								if "staff" in info:
									with doc.tag("ul"):
										for owner in info.get("staff", []):
											doc.line("li", owner.get("name"))
								else:
									doc.line("span", info.get("owner", {}).get("name"))

							with doc.tag("td", klass="title"):
								doc.line("span", info.get("title"))

			with doc.tag("script"):
				doc.asis(search_script)

	return doc.getvalue()


def main(args):
	out = sys.stdout
	logger.debug("output to " + (args.out or "stdout"))
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


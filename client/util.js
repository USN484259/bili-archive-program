import * as config from "/config.js";


export function make_player_url(path) {
	return config.player_url + "?path=" + path;
}

export function make_video_url(path) {
	return config.video_page_url + "?path=" + path;
}

export function make_proxy_url(url) {
	let proxy_url = new URL(config.bili_proxy_api, document.location)
	proxy_url.search = encodeURIComponent(url);

	console.log(proxy_url)
	return proxy_url.toString()
}

export function make_image_url(url, thumbnail = null) {
	let pos = url.indexOf('@');
	if (thumbnail) {
		if (pos < 0) {
			url += `@${thumbnail[0]}w_${thumbnail[1]}h_1c_!web-video-rcmd-cover.webp`
		}
	} else {
		if (pos >= 0) {
			url = url.substring(0, pos);
		}
	}
	let image_url = new URL(config.image_cache_api, document.location);
	image_url.search = encodeURIComponent(url);

	console.log(image_url)
	return image_url.toString()
}

export function size_string(size) {
	const size_unit = [
		" B",
		" KiB",
		" MiB",
		" GiB",
		" TiB",
		" PiB"
	];
	let index = 0
	let unit_base = 1
	while (size >= unit_base * 0x400) {
		if (index + 1 >= size_unit.length)
			break
		index += 1
		unit_base *= 0x400
	}
	return (size / unit_base).toFixed(index > 0) + size_unit[index]
}

export function duration_string(duration) {
	let result = ""
	if (duration >= 3600) {
		result += Math.floor(duration / 3600) + ':'
		duration %= 3600
	}

	let min = Math.floor(duration / 60)
	let sec = duration % 60
	result += min.toLocaleString(undefined, {minimumIntegerDigits: 2})
		+ ':' + sec.toLocaleString(undefined, {minimumIntegerDigits: 2})

	return result
}

export function stat_string(number) {
	const unit_table = ['', 'K', 'M', 'G', 'T']
	let unit_index = 0
	while (number > 1000) {
		number /= 1000
		unit_index += 1
	}
	if (unit_index == 0)
		return number.toString()
	else
		return number.toPrecision(3) + unit_table[unit_index]

}

const localtime = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "medium"});

export function localtime_string(time_ms) {
	return localtime.format(new Date(time_ms));
}

function on_schedule_video(ev) {
	ev.preventDefault();
	fetch(ev.target.action, {
		method: "POST",
		body: new URLSearchParams(new FormData(ev.target))
	}).then((resp) => resp.json()).then((reply) => {
		console.log(reply)
		let from_elem = ev.target
		for (let elem of from_elem.getElementsByTagName("input")) {
			if (elem.getAttribute("type") == "submit") {
				elem.setAttribute("value", "scheduled")
			}
		}
	})
}

export function create_schedule_button(bvid) {
	let form_elem = document.createElement("form");
	form_elem.setAttribute("method", "POST");
	form_elem.setAttribute("action", config.video_cache_api);

	let bvid_elem = document.createElement("input");
	bvid_elem.setAttribute("type", "hidden");
	bvid_elem.setAttribute("name", "bvid");
	bvid_elem.setAttribute("value", bvid);
	form_elem.appendChild(bvid_elem);

	let button_elem = document.createElement("input");
	button_elem.setAttribute("type", "submit");
	button_elem.setAttribute("value", "schedule");
	form_elem.appendChild(button_elem);

	form_elem.addEventListener("submit", on_schedule_video)
	return form_elem;
}

const cover_image_pattern = new RegExp("^(.+/)[^/.]+([.][^/.]+)$");

export function handle_legacy_cover_image(ev) {
	let img_elem = ev.target;
	img_elem.onerror = null;
	let cover_url = img_elem.src.replace(cover_image_pattern, "$1cover$2");
	img_elem.src = cover_url;
}

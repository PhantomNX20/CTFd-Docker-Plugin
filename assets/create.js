CTFd.plugin.run((_CTFd) => {
	const $ = _CTFd.lib.$;
	const md = _CTFd.lib.markdown();
});

var containerImage = document.getElementById("container-image");
var containerImageDefault = document.getElementById("container-image-default");
var path = "/containers/api/images";

var xhr = new XMLHttpRequest();
xhr.open("GET", path, true);
xhr.setRequestHeader("Accept", "application/json");
var csrfNonce = (window.init && init.csrfNonce) || (window.CTFd && CTFd.config && CTFd.config.csrfNonce) || "";
if (csrfNonce) {
	xhr.setRequestHeader("CSRF-Token", csrfNonce);
}
xhr.send();
xhr.onload = function () {
	var data = JSON.parse(this.responseText);
	if (data.error != undefined) {
		// Error
		containerImageDefault.innerHTML = data.error;
	} else {
		// Success
		for (var i = 0; i < data.images.length; i++) {
			var opt = document.createElement("option");
			opt.value = data.images[i];
			opt.innerHTML = data.images[i];
			containerImage.appendChild(opt);
		}
		containerImageDefault.innerHTML = "Choose an image...";
		containerImage.removeAttribute("disabled");
	}
	console.log(data);
};

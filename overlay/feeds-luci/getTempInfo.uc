// RAX1800-Z: expose the reference router's exact command
//     ubus call luci getTempInfo
//     -> {"tempinfo": "CPU: 49°C, WiFi0: 51°C, WiFi1: 48°C"}
// through the standard luci ubus object, which lives in this luci-base
// ucode plugin.  The aggregation itself (sysfs walk, N/A placeholders,
// WiFi slot padding) lives in /usr/libexec/rpcd/rax1800z_temp from
// luci-app-rax1800z-temp - called here as a plain executable, NOT via
// ubus, so no extra ACL is involved and the method degrades to a constant
// N/A when that package is absent instead of erroring the whole object.
//
// This file is appended by apply.sh right before the plugin's contract
// line `return { luci: methods };` (see the apply.sh step below).  It
// registers one more method on the existing methods object rather than
// replacing it, so upstream luci-base methods keep working untouched.
methods.getTempInfo = {
	call: function() {
		const fd = popen('/usr/libexec/rpcd/rax1800z_temp call getTempInfo 2>/dev/null', 'r');

		if (!fd)
			return { tempinfo: 'N/A' };

		const out = trim(fd.read('all'));

		fd.close();

		if (!length(out))
			return { tempinfo: 'N/A' };

		const parsed = json(out);

		return parsed ? parsed : { tempinfo: 'N/A' };
	}
};
